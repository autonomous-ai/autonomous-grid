"""Deploy a trained LoRA adapter back onto serving nodes (vLLM runtime LoRA endpoints).

Closing the loop: after a climb, the adapter must reach the engines that will serve it. v0 uses
vLLM's dynamic-adapter API — `POST /v1/load_lora_adapter {lora_name, lora_path}` — which reads
the adapter from a path visible to the *serving* process, so the artifacts directory must be on
a shared/synced path for multi-box fleets (documented limit; the artifact push plane is ADR 0019
phase 2). Re-deploying under the same name unloads first, so iterative climbs converge on one
stable model name the `auto` router can keep routing to.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx


class DeployError(SystemExit):
    def __init__(self, message: str) -> None:
        super().__init__(f"grid train: {message}")


def deploy_adapter(
    adapter_dir: str | Path,
    nodes: tuple[str, ...] | list[str],
    adapter_name: str,
    *,
    api_key_env: str = "GRID_TRAIN_API_KEY",
    transport: httpx.BaseTransport | None = None,
) -> list[dict]:
    """Load `adapter_dir` as `adapter_name` on every node; per-node results, no fail-fast."""
    adapter_dir = Path(adapter_dir).expanduser().resolve()
    if not (adapter_dir / "adapter_config.json").is_file():
        raise DeployError(f"{adapter_dir} is not a LoRA adapter (no adapter_config.json)")
    if not nodes:
        raise DeployError("no deploy nodes given ([deploy].nodes or --node)")
    if not adapter_name:
        raise DeployError("adapter name required ([deploy].adapter_name or --name)")

    headers = {}
    key = os.environ.get(api_key_env, "") if api_key_env else ""
    if key:
        headers["Authorization"] = f"Bearer {key}"

    # One implementation of "load this adapter on that machine", in train/sync.py: it tries the
    # MLX engine's /reload_adapter first and falls back to vLLM's runtime-LoRA API. Deploying and
    # mid-run syncing are the same act at different moments, so they must not drift apart — an
    # earlier split version silently could not deploy to a Mac fleet at all.
    from .sync import push_adapter

    results = push_adapter(
        adapter_dir, list(nodes), adapter_name=adapter_name,
        api_key_env=api_key_env, transport=transport,
    )
    for result in results:
        if result["ok"]:
            result["detail"] = f"serving as {adapter_name!r}"
    if any(r["ok"] for r in results):
        record_deploy(adapter_name, adapter_dir)
    return results


# --- what each name was last loaded from ---------------------------------------------------
# Engines cannot be asked "which weights are you holding". Without a record, a check that has to
# displace the customer's model (a one-slot engine — Grid's own MLX server is one) has nothing to
# put back when the candidate loses.

def _ledger_path() -> Path:
    from .run import artifacts_root

    return artifacts_root() / "deployed.json"


def record_deploy(adapter_name: str, adapter_dir: str | Path) -> None:
    """Remember that `adapter_name` is now this directory. Never raises: a lost note is not
    a reason to fail a deploy that worked."""
    import json

    path = _ledger_path()
    try:
        known = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        if not isinstance(known, dict):
            known = {}
        known[adapter_name] = str(Path(adapter_dir).expanduser().resolve())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(known, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError):
        pass


def last_deployed(adapter_name: str) -> Path | None:
    """Where `adapter_name` was last loaded from, if we know and it is still there."""
    import json

    path = _ledger_path()
    try:
        known = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    where = known.get(adapter_name) if isinstance(known, dict) else None
    return Path(where) if where and Path(where).is_dir() else None
