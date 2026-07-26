"""Adapter conversion tests: peft <-> mlx round trip, shapes, configs, error paths.

Synthetic tensors — no torch, no mlx, no model download — because what must be right is the
key mapping and the transpose, and those are checkable exactly.
"""
from __future__ import annotations

import json

import numpy as np
import pytest
from safetensors.numpy import load_file, save_file

from train.adapters import (
    convert,
    detect_format,
    mlx_to_peft_tensors,
    peft_to_mlx_tensors,
)

IN_FEATURES, OUT_FEATURES, RANK = 8, 12, 4


def _peft_adapter(tmp_path):
    d = tmp_path / "peft"
    d.mkdir()
    tensors = {}
    for layer in (0, 1):
        for proj in ("q_proj", "v_proj"):
            stem = f"base_model.model.model.layers.{layer}.self_attn.{proj}"
            tensors[f"{stem}.lora_A.weight"] = np.arange(
                RANK * IN_FEATURES, dtype=np.float32
            ).reshape(RANK, IN_FEATURES)
            tensors[f"{stem}.lora_B.weight"] = np.arange(
                OUT_FEATURES * RANK, dtype=np.float32
            ).reshape(OUT_FEATURES, RANK)
    save_file(tensors, str(d / "adapter_model.safetensors"))
    (d / "adapter_config.json").write_text(
        json.dumps({"peft_type": "LORA", "r": RANK, "lora_alpha": 2 * RANK}), encoding="utf-8"
    )
    return d, tensors


def _mlx_adapter(tmp_path):
    d = tmp_path / "mlx"
    d.mkdir()
    tensors = {}
    for layer in (0, 1):
        stem = f"model.layers.{layer}.self_attn.q_proj"
        tensors[f"{stem}.lora_a"] = np.ones((IN_FEATURES, RANK), dtype=np.float32)
        tensors[f"{stem}.lora_b"] = np.ones((RANK, OUT_FEATURES), dtype=np.float32) * 2
    save_file(tensors, str(d / "adapters.safetensors"))
    (d / "adapter_config.json").write_text(
        json.dumps({"fine_tune_type": "lora", "num_layers": 2,
                    "lora_parameters": {"rank": RANK, "scale": 20.0}}),
        encoding="utf-8",
    )
    return d, tensors


def test_peft_to_mlx_keys_and_shapes(tmp_path):
    _, tensors = _peft_adapter(tmp_path)
    out = peft_to_mlx_tensors(tensors)
    key = "model.layers.0.self_attn.q_proj.lora_a"
    assert key in out
    # peft [r, in] -> mlx [in, r]; peft [out, r] -> mlx [r, out]
    assert out[key].shape == (IN_FEATURES, RANK)
    assert out["model.layers.0.self_attn.q_proj.lora_b"].shape == (RANK, OUT_FEATURES)
    assert not any(k.startswith("base_model.") for k in out)


def test_round_trip_is_exact(tmp_path):
    _, tensors = _peft_adapter(tmp_path)
    back = mlx_to_peft_tensors(peft_to_mlx_tensors(tensors))
    assert set(back) == {k for k in tensors if ".lora_" in k}
    for key, value in back.items():
        np.testing.assert_array_equal(value, tensors[key])


def test_convert_peft_dir_to_mlx(tmp_path):
    source, _ = _peft_adapter(tmp_path)
    dest = tmp_path / "out-mlx"
    assert convert(source, dest) == "mlx"
    assert detect_format(dest) == "mlx"
    written = load_file(str(dest / "adapters.safetensors"))
    assert written["model.layers.1.self_attn.v_proj.lora_a"].shape == (IN_FEATURES, RANK)
    config = json.loads((dest / "adapter_config.json").read_text())
    assert config["fine_tune_type"] == "lora"
    assert config["lora_parameters"]["rank"] == RANK
    assert config["lora_parameters"]["scale"] == 2.0  # peft alpha/r = 8/4
    assert config["num_layers"] == 2


def test_convert_mlx_dir_to_peft(tmp_path):
    source, _ = _mlx_adapter(tmp_path)
    dest = tmp_path / "out-peft"
    assert convert(source, dest) == "peft"
    assert detect_format(dest) == "peft"
    written = load_file(str(dest / "adapter_model.safetensors"))
    key = "base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight"
    assert written[key].shape == (RANK, IN_FEATURES)
    config = json.loads((dest / "adapter_config.json").read_text())
    assert config["r"] == RANK and config["lora_alpha"] == 20.0 * RANK
    assert config["target_modules"] == ["q_proj"]


def test_detect_format_errors_clearly(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    with pytest.raises(SystemExit, match="no recognizable adapter"):
        detect_format(empty)


def test_convert_refuses_same_format(tmp_path):
    source, _ = _peft_adapter(tmp_path)
    with pytest.raises(SystemExit, match="already peft"):
        convert(source, tmp_path / "x", to="peft")


def test_conversion_ignores_non_lora_tensors():
    out = peft_to_mlx_tensors(
        {
            "base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight": np.zeros((2, 3), np.float32),
            "base_model.model.model.layers.0.self_attn.q_proj.lora_B.weight": np.zeros((4, 2), np.float32),
            "base_model.model.score.modules_to_save.weight": np.zeros((5, 5), np.float32),
        }
    )
    assert set(out) == {
        "model.layers.0.self_attn.q_proj.lora_a",
        "model.layers.0.self_attn.q_proj.lora_b",
    }


def test_a_config_less_round_trip_does_not_move_the_scale(tmp_path):
    """peft -> mlx fell back to mlx-lm's example 20.0 while mlx -> peft fell back to 2.0, so an
    adapter converted twice came back meaning something ten times different. Same defect that
    inverted the M2 trainer (5790516) and made the SFT lane train a rank nobody asked for."""
    from train.adapters import DEFAULT_SCALE

    source, _ = _peft_adapter(tmp_path)
    (source / "adapter_config.json").unlink()          # an adapter whose config we cannot read

    mlx = tmp_path / "out-mlx"
    convert(source, mlx, to="mlx")
    scale = json.loads((mlx / "adapter_config.json").read_text())["lora_parameters"]["scale"]
    assert scale == DEFAULT_SCALE

    (mlx / "adapter_config.json").unlink()
    back = tmp_path / "out-peft"
    convert(mlx, back, to="peft")
    cfg = json.loads((back / "adapter_config.json").read_text())
    assert cfg["lora_alpha"] / cfg["r"] == DEFAULT_SCALE       # the same adapter, still
