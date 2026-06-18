"""Detect inference engines already running on this box.

Used by `grid join` (no engine flags) to discover what this machine already
serves. Each probe is a short, best-effort HTTP request to a well-known local
port; anything unreachable is simply skipped. Probes run in the documented
priority order: Ollama, LM Studio, vLLM, MLX, llama.cpp, ComfyUI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

import runtime


@dataclass(frozen=True)
class DetectedEngine:
    label: str
    endpoint_url: str
    models: list[str] = field(default_factory=list)
    media: bool = False


# (label, port, kind) — `kind` selects how we read the model list.
#   "openai" -> GET /v1/models, data[].id
#   "ollama" -> GET /api/tags,   models[].name (served via OpenAI proxy at /v1)
#   "comfyui" -> GET /system_stats reachable -> media engine (no /v1 models)
_PROBES: tuple[tuple[str, int, str], ...] = (
    ("ollama", 11434, "ollama"),
    ("lm-studio", 1234, "openai"),
    ("vllm", 8000, "openai"),
    ("mlx", 8080, "openai"),
    ("llama.cpp", 8081, "openai"),
    ("comfyui", 8188, "comfyui"),
)


def detect_engines(*, advertise_host: str | None = None, timeout: float = 0.75) -> list[DetectedEngine]:
    """Probe localhost for running engines and return the ones that answer."""
    found: list[DetectedEngine] = []
    for label, port, kind in _PROBES:
        if kind == "comfyui":
            if _comfyui_reachable(port, timeout):
                # Advertise media by this box's LAN host; the media bundle is
                # selected at join time, so no model list here.
                url = runtime.provider_endpoint_url(None, port, advertise_host).removesuffix("/v1")
                found.append(DetectedEngine(label=label, endpoint_url=url, models=[], media=True))
            continue
        models = _probe(port, kind, timeout)
        if models is None:
            continue
        # Advertise the engine by this box's LAN IP, not localhost, so other
        # machines on the grid can reach it.
        endpoint_url = runtime.provider_endpoint_url(None, port, advertise_host)
        found.append(DetectedEngine(label=label, endpoint_url=endpoint_url, models=models))
    return found


def _probe(port: int, kind: str, timeout: float) -> list[str] | None:
    if kind == "ollama":
        models = _read_json_list(f"http://127.0.0.1:{port}/api/tags", "models", "name", timeout)
        if models is not None:
            return models
        # Newer Ollama also speaks the OpenAI API; fall through to that shape.
    return _read_json_list(f"http://127.0.0.1:{port}/v1/models", "data", "id", timeout)


def _comfyui_reachable(port: int, timeout: float) -> bool:
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/system_stats", timeout=timeout)
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


def _read_json_list(url: str, container: str, key: str, timeout: float) -> list[str] | None:
    try:
        resp = httpx.get(url, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    items = payload.get(container) if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return None
    return [str(item[key]) for item in items if isinstance(item, dict) and item.get(key)]
