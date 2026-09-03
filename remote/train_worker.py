"""Capability probe and argv construction for durable local SFT task workers.

Training is a typed task, not an agent prompt. The relay authors a small JSON spec; the provider
validates it again and invokes Grid's installed trainer without a shell. Input data and result
adapters travel through the task worktree, while model caches stay local to the node.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import platform
import sys
import tomllib
from pathlib import Path
from typing import Any

BACKENDS = {"train-mlx": "mlx", "train-torch": "torch"}
_TORCH_MODULES = ("torch", "transformers", "trl", "peft", "datasets")
_MAX_ITERS = 10_000_000
MIN_RUN_TIMEOUT_SECONDS = 3_600
MAX_RUN_TIMEOUT_SECONDS = 7 * 24 * 3_600
MIN_QUEUE_TIMEOUT_SECONDS = 3_600
MAX_QUEUE_TIMEOUT_SECONDS = 30 * 24 * 3_600


def available(agent_kind: str) -> bool:
    """Whether this interpreter can execute the advertised training backend."""
    backend = BACKENDS.get(agent_kind)
    if backend == "mlx":
        return (platform.system() == "Darwin" and platform.machine() == "arm64"
                and importlib.util.find_spec("mlx_lm") is not None)
    if backend == "torch":
        return all(importlib.util.find_spec(name) is not None for name in _TORCH_MODULES)
    return False


def preflight(agent_kind: str) -> None:
    """Fail with one actionable sentence before this worker advertises capacity."""
    if agent_kind not in BACKENDS:
        raise RuntimeError(f"unsupported training worker {agent_kind!r}")
    if available(agent_kind):
        return
    if agent_kind == "train-mlx":
        raise RuntimeError("train-mlx needs Apple Silicon and mlx-lm (install `grid[train]`)")
    raise RuntimeError(
        "train-torch needs torch, transformers, trl, peft and datasets "
        "(install `grid[train]`)")


def spec(job: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Validate one relay-authored training spec and resolve its confined paths."""
    kind = str(job.get("agent_kind") or "")
    backend = BACKENDS.get(kind)
    if job.get("kind") != "train.sft" or backend is None:
        raise ValueError("training workers only accept train.sft jobs for their exact backend")
    try:
        spec = json.loads(str(job.get("prompt") or ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("training job has a malformed JSON spec") from exc
    if not isinstance(spec, dict) or set(spec) - {
            "version", "backend", "config", "run_dir", "iters",
            "run_timeout_seconds", "queue_timeout_seconds"}:
        raise ValueError("training job spec has unknown fields or is not an object")
    if spec.get("version") != 1 or spec.get("backend") != backend:
        raise ValueError("training job spec version or backend does not match its worker")

    config = _inside(workspace, spec.get("config", "grid-train-input/grid-train.toml"))
    run_dir = _inside(workspace, spec.get("run_dir", "grid-train-result"))
    if not config.is_file():
        raise ValueError(f"training config is missing: {config.relative_to(workspace)}")
    iters = spec.get("iters")
    if (iters is not None and (not isinstance(iters, int) or isinstance(iters, bool)
                              or not 1 <= iters <= _MAX_ITERS)):
        raise ValueError(f"training iters must be 1-{_MAX_ITERS}")
    if iters is not None and backend != "mlx":
        raise ValueError("training iters only applies to the mlx backend")

    run_timeout = _bounded_seconds(
        spec.get("run_timeout_seconds", 24 * 3_600),
        "run timeout", MIN_RUN_TIMEOUT_SECONDS, MAX_RUN_TIMEOUT_SECONDS)
    queue_timeout = _bounded_seconds(
        spec.get("queue_timeout_seconds", 7 * 24 * 3_600),
        "queue timeout", MIN_QUEUE_TIMEOUT_SECONDS, MAX_QUEUE_TIMEOUT_SECONDS)
    if queue_timeout <= run_timeout:
        raise ValueError("training queue timeout must be greater than its run timeout")
    claimed_timeout = job.get("run_timeout_seconds")
    if claimed_timeout is not None and claimed_timeout != run_timeout:
        raise ValueError("training run timeout does not match the relay claim")
    return {
        **spec, "backend": backend, "config_path": config, "run_dir_path": run_dir,
        "run_timeout_seconds": run_timeout, "queue_timeout_seconds": queue_timeout,
    }


def command(job: dict[str, Any], workspace: Path) -> list[str]:
    """Return a shell-free command after validating the relay-authored spec and its paths."""
    parsed = spec(job, workspace)
    backend = parsed["backend"]
    config = parsed["config_path"]
    run_dir = parsed["run_dir_path"]
    iters = parsed.get("iters")

    # -I keeps the checked-out project from shadowing Grid's installed ``cli`` package.
    argv = [
        sys.executable, "-I", "-m", "cli", "train", "sft",
        "--config", str(config), "--backend", backend, "--run-dir", str(run_dir),
    ]
    if iters is not None:
        argv += ["--iters", str(iters)]
    return argv


def timeout(job: dict[str, Any], workspace: Path) -> float:
    """The relay-authorized wall-clock budget for this exact training job."""
    return float(spec(job, workspace)["run_timeout_seconds"])


def finalize_result(job: dict[str, Any], workspace: Path) -> dict[str, Any]:
    """Refuse false success and write a portable, checksummed adapter manifest."""
    parsed = spec(job, workspace)
    run_dir = parsed["run_dir_path"]
    adapter = run_dir / "adapter"
    run_file = run_dir / "run.json"
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise ValueError("training completed without its result directory")
    if adapter.is_symlink() or not adapter.is_dir():
        raise ValueError("training completed without an adapter directory")
    try:
        run = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("training completed without a valid run.json") from exc
    if not isinstance(run, dict) or run.get("stage") != "sft":
        raise ValueError("training run.json does not describe an SFT run")
    if run.get("backend") != parsed["backend"]:
        raise ValueError("training result backend does not match the claimed worker")
    try:
        configured = tomllib.loads(parsed["config_path"].read_text(encoding="utf-8"))
        expected_model = configured["model"]["name"]
    except (KeyError, OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, TypeError) as exc:
        raise ValueError("training input has no valid base model") from exc
    if not isinstance(expected_model, str) or not expected_model or run.get("model") != expected_model:
        raise ValueError("training result model does not match its immutable config")

    files = []
    for path in sorted(adapter.rglob("*")):
        if path.is_symlink():
            raise ValueError("training adapter contains a symbolic link")
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        files.append({
            "path": path.relative_to(run_dir).as_posix(),
            "bytes": size,
            "sha256": digest.hexdigest(),
        })
    if not files:
        raise ValueError("training completed with an empty adapter directory")

    # A worker's absolute task path is meaningless after `grid task fetch`. Keep the existing
    # run record useful by making the adapter location relative to that record.
    run["adapter"] = "adapter"
    run_file.write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest = {
        "version": 1,
        "kind": "train.sft",
        "backend": parsed["backend"],
        "model": expected_model,
        "adapter": "adapter",
        "run": "run.json",
        "files": files,
        "total_bytes": sum(item["bytes"] for item in files),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def verify_result(path: Path) -> dict[str, Any]:
    """Verify a fetched distributed-training result against its portable manifest."""
    supplied = path.expanduser()
    if supplied.is_symlink() or not supplied.is_dir():
        raise ValueError(f"training result is missing or unsafe: {supplied}")
    root = supplied.resolve()
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("training result manifest is unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"no valid training manifest at {manifest_path}") from exc
    if (not isinstance(manifest, dict) or manifest.get("version") != 1
            or manifest.get("kind") != "train.sft"):
        raise ValueError("unsupported training result manifest")
    backend = manifest.get("backend")
    model = manifest.get("model")
    if (backend not in BACKENDS.values() or not isinstance(model, str) or not model
            or manifest.get("adapter") != "adapter" or manifest.get("run") != "run.json"):
        raise ValueError("training result manifest has invalid run metadata")
    run_path = root / "run.json"
    if run_path.is_symlink():
        raise ValueError("training run record is unsafe")
    try:
        run = json.loads(run_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("training result has no valid run.json") from exc
    if (not isinstance(run, dict) or run.get("stage") != "sft"
            or run.get("backend") != backend or run.get("model") != model
            or run.get("adapter") != "adapter"):
        raise ValueError("training run record does not match its manifest")
    adapter_root = root / "adapter"
    if adapter_root.is_symlink() or not adapter_root.is_dir():
        raise ValueError("training adapter directory is missing or unsafe")
    expected = manifest.get("files")
    if not isinstance(expected, list) or not expected:
        raise ValueError("training result manifest lists no adapter files")
    seen: set[str] = set()
    total = 0
    for item in expected:
        if not isinstance(item, dict) or set(item) != {"path", "bytes", "sha256"}:
            raise ValueError("training result manifest has a malformed file record")
        relative = item["path"]
        expected_bytes = item["bytes"]
        expected_digest = item["sha256"]
        if (not isinstance(relative, str) or not relative.startswith("adapter/")
                or relative in seen):
            raise ValueError("training result manifest has an unsafe or duplicate adapter path")
        if (not isinstance(expected_bytes, int) or isinstance(expected_bytes, bool)
                or expected_bytes < 0 or not isinstance(expected_digest, str)
                or len(expected_digest) != 64
                or any(character not in "0123456789abcdef" for character in expected_digest)):
            raise ValueError("training result manifest has a malformed file record")
        seen.add(relative)
        artifact = _inside(root, relative)
        if artifact.is_symlink() or not artifact.is_file():
            raise ValueError(f"training adapter file is missing or unsafe: {relative}")
        digest = hashlib.sha256()
        size = 0
        with artifact.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        if size != expected_bytes or digest.hexdigest() != expected_digest:
            raise ValueError(f"training adapter file failed verification: {relative}")
        total += size
    actual = {
        path.relative_to(root).as_posix()
        for path in adapter_root.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if actual != seen:
        raise ValueError("training adapter contains files not covered by its manifest")
    manifest_total = manifest.get("total_bytes")
    if (not isinstance(manifest_total, int) or isinstance(manifest_total, bool)
            or manifest_total != total):
        raise ValueError("training result manifest total does not match its files")
    return manifest


def _bounded_seconds(value: object, name: str, minimum: int, maximum: int) -> int:
    if (not isinstance(value, int) or isinstance(value, bool)
            or not minimum <= value <= maximum):
        raise ValueError(f"training {name} must be {minimum}-{maximum} seconds")
    return value


def _inside(workspace: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or value.startswith(("/", ".")):
        raise ValueError("training paths must be safe relative paths")
    parts = Path(value).parts
    if ".." in parts:
        raise ValueError("training paths must stay inside the task workspace")
    root = workspace.resolve()
    answer = (root / value).resolve()
    if answer != root and root not in answer.parents:
        raise ValueError("training paths must stay inside the task workspace")
    return answer
