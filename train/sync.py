"""Weight sync: push the trainer's adapter to the engines serving rollouts.

Without this, remote rollouts come from the frozen base model forever — the trainer learns
off-policy from stale samples and the reward curve never moves. With it, the loop closes:
train N steps → save adapter → tell each engine to reload it → rollouts now come from the
improved policy. Adapter deltas are tens of MB, so on a LAN this costs about a second.

Two transports, both HTTP:
  - `/reload_adapter {adapter_dir}`   — Grid's MLX rollout server (path must be reachable by it)
  - `/load_lora_adapter {lora_name, lora_path}` — vLLM's runtime-LoRA API (see train/deploy.py)

Failure policy: a node that can't reload is reported and SKIPPED, never fatal. One sleeping
laptop must not kill a training run — that is the whole premise of pooling personal machines.
"""
from __future__ import annotations

import os
from pathlib import Path

import httpx


def push_adapter(
    adapter_dir: str | Path,
    nodes: list[str] | tuple[str, ...],
    *,
    adapter_name: str = "grid-train",
    api_key_env: str = "GRID_TRAIN_API_KEY",
    timeout: float = 120.0,
    transport: httpx.BaseTransport | None = None,
) -> list[dict]:
    """Reload `adapter_dir` on every node. Returns per-node {node, ok, detail}."""
    adapter_dir = str(Path(adapter_dir).expanduser().resolve())
    headers = {}
    key = os.environ.get(api_key_env, "") if api_key_env else ""
    if key:
        headers["Authorization"] = f"Bearer {key}"

    results = []
    for node in nodes:
        base = node.rstrip("/")
        # The MLX server mounts /reload_adapter at the server root, not under /v1.
        root = base.removesuffix("/v1")
        try:
            with httpx.Client(headers=headers, timeout=timeout, transport=transport) as client:
                response = client.post(
                    f"{root}/reload_adapter",
                    # `name` is what the engine will answer for afterwards. Omitting it left the
                    # engine serving the adapter under its directory name while the gate asked for
                    # the model's name — a 404 exactly at the moment of payoff.
                    json={"adapter_dir": adapter_dir, "name": adapter_name},
                )
                if response.status_code == 404:
                    # Not an MLX rollout server — try vLLM's runtime-LoRA API.
                    response = client.post(
                        f"{base}/load_lora_adapter",
                        json={"lora_name": adapter_name, "lora_path": adapter_dir},
                    )
        except httpx.TransportError as exc:
            results.append({"node": base, "ok": False, "detail": f"unreachable: {exc}"})
            continue
        if response.status_code == 200:
            results.append({"node": base, "ok": True, "detail": "adapter reloaded"})
        else:
            results.append(
                {
                    "node": base,
                    "ok": False,
                    "detail": f"HTTP {response.status_code}: {response.text[:160]}",
                }
            )
    return results


def summarize(results: list[dict]) -> str:
    """One line for a training log: '2/3 nodes synced (1 skipped)'."""
    ok = sum(1 for r in results if r["ok"])
    total = len(results)
    tail = "" if ok == total else f" ({total - ok} skipped)"
    return f"{ok}/{total} nodes synced{tail}"
