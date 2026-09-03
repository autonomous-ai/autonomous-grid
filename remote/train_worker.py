"""Capability probe and argv construction for durable local SFT task workers.

Training is a typed task, not an agent prompt. The relay authors a small JSON spec; the provider
validates it again and invokes Grid's installed trainer without a shell. Input data and result
adapters travel through the task worktree, while model caches stay local to the node.
"""
from __future__ import annotations

import importlib.util
import json
import platform
import sys
from pathlib import Path
from typing import Any

BACKENDS = {"train-mlx": "mlx", "train-torch": "torch"}
_TORCH_MODULES = ("torch", "transformers", "trl", "peft", "datasets")
_MAX_ITERS = 10_000_000


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


def command(job: dict[str, Any], workspace: Path) -> list[str]:
    """Return a shell-free command after validating the relay-authored spec and its paths."""
    kind = str(job.get("agent_kind") or "")
    backend = BACKENDS.get(kind)
    if job.get("kind") != "train.sft" or backend is None:
        raise ValueError("training workers only accept train.sft jobs for their exact backend")
    try:
        spec = json.loads(str(job.get("prompt") or ""))
    except (TypeError, ValueError) as exc:
        raise ValueError("training job has a malformed JSON spec") from exc
    if not isinstance(spec, dict) or set(spec) - {
            "version", "backend", "config", "run_dir", "iters"}:
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

    # -I keeps the checked-out project from shadowing Grid's installed ``cli`` package.
    argv = [
        sys.executable, "-I", "-m", "cli", "train", "sft",
        "--config", str(config), "--backend", backend, "--run-dir", str(run_dir),
    ]
    if iters is not None:
        argv += ["--iters", str(iters)]
    return argv


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
