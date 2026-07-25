"""LoRA adapter conversion between the torch/peft and MLX formats.

Why this exists: a heterogeneous fleet (a CUDA or Intel trainer + Apple-Silicon rollout nodes)
trains in one format and serves in another. Both store the same thing — a low-rank pair per
adapted linear layer — but with different key names and transposed layouts, so a straight file
copy silently fails to load. This module is the bridge, and it is the reason "pool every machine
you own" can be true rather than aspirational.

Shapes (verified against peft's LoraLayer and mlx-lm's LoRALinear):
  peft:  base_model.model.<path>.lora_A.weight  [r, in_features]
         base_model.model.<path>.lora_B.weight  [out_features, r]
  mlx:   <path>.lora_a                          [in_features, r]
         <path>.lora_b                          [r, out_features]
So each direction is a rename plus a transpose. Conversion is exact — no precision loss beyond
the dtype cast the caller asks for.

Pure-numpy on purpose (safetensors only): it runs on a machine with neither torch nor mlx
installed, which is what lets the trainer prepare a Mac-shaped adapter without importing MLX.
"""
from __future__ import annotations

from pathlib import Path

_PEFT_PREFIX = "base_model.model."


class AdapterError(SystemExit):
    def __init__(self, message: str) -> None:
        super().__init__(f"grid train (adapters): {message}")


def _load_safetensors(path: Path) -> dict:
    try:
        from safetensors.numpy import load_file
    except ImportError as exc:  # safetensors ships with transformers/mlx-lm
        raise AdapterError(f"needs the safetensors package ({exc.name})") from exc
    return load_file(str(path))


def _save_safetensors(tensors: dict, path: Path) -> None:
    from safetensors.numpy import save_file

    save_file(tensors, str(path))


def peft_to_mlx_tensors(tensors: dict) -> dict:
    """{peft key: array} -> {mlx key: array}, transposing each factor."""
    out: dict = {}
    for key, value in tensors.items():
        if ".lora_A" not in key and ".lora_B" not in key:
            continue  # peft also stores non-LoRA extras (e.g. modules_to_save); MLX has no slot
        path = key
        path = path.removeprefix(_PEFT_PREFIX)
        # peft: <path>.lora_A.weight (also .default.weight in some versions)
        for suffix, target in ((".lora_A", "lora_a"), (".lora_B", "lora_b")):
            if suffix in path:
                stem = path.split(suffix)[0]
                out[f"{stem}.{target}"] = value.T
                break
    if not out:
        raise AdapterError("no LoRA tensors found — is this a peft adapter?")
    return out


def mlx_to_peft_tensors(tensors: dict) -> dict:
    """{mlx key: array} -> {peft key: array}, transposing each factor."""
    out: dict = {}
    for key, value in tensors.items():
        for suffix, target in ((".lora_a", "lora_A"), (".lora_b", "lora_B")):
            if key.endswith(suffix):
                stem = key[: -len(suffix)]
                out[f"{_PEFT_PREFIX}{stem}.{target}.weight"] = value.T
                break
    if not out:
        raise AdapterError("no LoRA tensors found — is this an mlx-lm adapter?")
    return out


def detect_format(adapter_dir: Path) -> str:
    """'peft' | 'mlx' — by the files each writes."""
    if (adapter_dir / "adapter_model.safetensors").is_file():
        return "peft"
    if (adapter_dir / "adapters.safetensors").is_file():
        return "mlx"
    raise AdapterError(
        f"{adapter_dir} holds no recognizable adapter "
        "(expected adapter_model.safetensors or adapters.safetensors)"
    )


def convert(source_dir: str | Path, dest_dir: str | Path, *, to: str | None = None) -> str:
    """Convert an adapter directory into the other backend's format.

    `to` may be 'mlx' or 'peft'; omitted means "the format the source is not". Returns the
    format written. Config files are written too, so the destination loads as-is.
    """
    source_dir = Path(source_dir).expanduser()
    dest_dir = Path(dest_dir).expanduser()
    source_format = detect_format(source_dir)
    target = to or ("mlx" if source_format == "peft" else "peft")
    if target == source_format:
        raise AdapterError(f"source is already {target} format")
    dest_dir.mkdir(parents=True, exist_ok=True)

    if target == "mlx":
        tensors = peft_to_mlx_tensors(_load_safetensors(source_dir / "adapter_model.safetensors"))
        _save_safetensors(tensors, dest_dir / "adapters.safetensors")
        _write_mlx_config(source_dir, dest_dir, tensors)
    else:
        tensors = mlx_to_peft_tensors(_load_safetensors(source_dir / "adapters.safetensors"))
        _save_safetensors(tensors, dest_dir / "adapter_model.safetensors")
        _write_peft_config(source_dir, dest_dir, tensors)
    return target


def _rank_from(tensors: dict) -> int:
    """LoRA rank = the small dimension of any lora_a/lora_A tensor."""
    for key, value in tensors.items():
        if key.endswith("lora_a") or ".lora_A" in key:
            return int(min(value.shape))
    raise AdapterError("cannot infer LoRA rank")


def _write_mlx_config(source_dir: Path, dest_dir: Path, tensors: dict) -> None:
    import json

    rank = _rank_from(tensors)
    scale = 20.0
    peft_config = source_dir / "adapter_config.json"
    if peft_config.is_file():
        raw = json.loads(peft_config.read_text(encoding="utf-8"))
        alpha = raw.get("lora_alpha")
        r = raw.get("r") or rank
        if alpha and r:
            # peft scales by alpha/r; mlx-lm multiplies by `scale` directly.
            scale = float(alpha) / float(r)
    layers = len({key.split(".layers.")[1].split(".")[0] for key in tensors if ".layers." in key})
    (dest_dir / "adapter_config.json").write_text(
        json.dumps(
            {
                "fine_tune_type": "lora",
                "num_layers": layers or 8,
                "lora_parameters": {"rank": rank, "scale": scale, "dropout": 0.0},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_peft_config(source_dir: Path, dest_dir: Path, tensors: dict) -> None:
    import json

    rank = _rank_from(tensors)
    alpha = 2 * rank
    mlx_config = source_dir / "adapter_config.json"
    if mlx_config.is_file():
        raw = json.loads(mlx_config.read_text(encoding="utf-8"))
        params = raw.get("lora_parameters") or {}
        rank = int(params.get("rank", rank))
        alpha = float(params.get("scale", 2.0)) * rank
    (dest_dir / "adapter_config.json").write_text(
        json.dumps(
            {
                "peft_type": "LORA",
                "task_type": "CAUSAL_LM",
                "r": rank,
                "lora_alpha": alpha,
                "lora_dropout": 0.0,
                # peft keys look like <...>.<module>.lora_A.weight — the module name is the
                # segment before ".lora_", not before ".weight".
                "target_modules": sorted(
                    {
                        module
                        for key in tensors
                        if ".lora_" in key
                        and (module := key.split(".lora_")[0].rsplit(".", 1)[-1])
                    }
                )
                or ["q_proj", "v_proj"],
                "bias": "none",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
