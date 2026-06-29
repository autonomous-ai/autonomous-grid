"""Detect inference engines already running on this box.

Used by `grid join` (no engine flags) to discover what this machine already
serves. Each probe is a short, best-effort HTTP request to a well-known local
port; anything unreachable is simply skipped. Probes run in the documented
priority order: Ollama, LM Studio, vLLM, MLX, llama.cpp, ComfyUI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from lan import runtime


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
    lan_host = advertise_host or runtime.detect_lan_ip()
    found: list[DetectedEngine] = []
    for label, port, kind in _PROBES:
        if kind == "comfyui":
            if _comfyui_reachable("127.0.0.1", port, timeout):
                host = _reachable_host(lan_host, advertise_host, port, kind, timeout)
                found.append(DetectedEngine(label=label, endpoint_url=f"http://{host}:{port}", models=[], media=True))
            continue
        models = _probe("127.0.0.1", port, kind, timeout)
        if models is None:
            continue
        host = _reachable_host(lan_host, advertise_host, port, kind, timeout)
        found.append(DetectedEngine(label=label, endpoint_url=f"http://{host}:{port}/v1", models=models))
    return found


def _reachable_host(lan_host: str, advertise_host: str | None, port: int, kind: str, timeout: float) -> str:
    """Pick the host to advertise.

    Prefer the LAN address so other machines can reach the engine, but only if
    the engine is actually bound there — many engines default to loopback. When
    it is not, keep `127.0.0.1` (correct when the grid runs on this same box).
    An explicit --advertise-host is always trusted.
    """
    if advertise_host:
        return lan_host
    if lan_host == "127.0.0.1":
        return "127.0.0.1"
    reachable = _comfyui_reachable(lan_host, port, timeout) if kind == "comfyui" else _probe(lan_host, port, kind, timeout) is not None
    return lan_host if reachable else "127.0.0.1"


def _probe(host: str, port: int, kind: str, timeout: float) -> list[str] | None:
    if kind == "ollama":
        models = _read_json_list(f"http://{host}:{port}/api/tags", "models", "name", timeout)
        if models is not None:
            return models
        # Newer Ollama also speaks the OpenAI API; fall through to that shape.
    return _read_json_list(f"http://{host}:{port}/v1/models", "data", "id", timeout)


def _comfyui_reachable(host: str, port: int, timeout: float) -> bool:
    try:
        resp = httpx.get(f"http://{host}:{port}/system_stats", timeout=timeout)
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
