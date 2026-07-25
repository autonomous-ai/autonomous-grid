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

    results = []
    for node in nodes:
        base = node.rstrip("/")
        with httpx.Client(base_url=base, headers=headers, timeout=60.0, transport=transport) as client:
            results.append(_deploy_one(client, base, adapter_dir, adapter_name))
    return results


def _deploy_one(client: httpx.Client, base: str, adapter_dir: Path, name: str) -> dict:
    body = {"lora_name": name, "lora_path": str(adapter_dir)}
    try:
        # Idempotent re-deploy: drop any previous adapter under this name, ignore "not found".
        client.post("/unload_lora_adapter", json={"lora_name": name})
        response = client.post("/load_lora_adapter", json=body)
    except httpx.TransportError as exc:
        return {"node": base, "ok": False, "detail": f"unreachable: {exc}"}
    if response.status_code == 404:
        return {
            "node": base,
            "ok": False,
            "detail": (
                "no runtime LoRA endpoint — start vLLM with VLLM_ALLOW_RUNTIME_LORA_UPDATING=True "
                "and --enable-lora"
            ),
        }
    if response.status_code != 200:
        return {"node": base, "ok": False, "detail": f"HTTP {response.status_code}: {response.text[:200]}"}
    return {"node": base, "ok": True, "detail": f"serving as {name!r}"}
