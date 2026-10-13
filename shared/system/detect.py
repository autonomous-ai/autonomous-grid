"""Detect inference engines already running on this box.

Used by `grid join` (no engine flags) to discover what this machine already
serves. Each probe is a short, best-effort HTTP request to a well-known local
port; anything unreachable is simply skipped. Probes run in the documented
priority order: Ollama, LM Studio, vLLM, MLX, llama.cpp, ComfyUI.

`openai_base` answers the same question for an engine named with `--at`: the
base it serves its OpenAI API from.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from local import runtime


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

# How many times `openai_base` asks an engine whose `/v1` has not answered yet, and how far apart: one still loading
# its model answers 5xx on every route, and one still starting refuses the connection.
_V1_ATTEMPTS = 3
_V1_RETRY_SECONDS = 1.0


def detect_engines(*, advertise_host: str | None = None, timeout: float = 0.75) -> list[DetectedEngine]:
    """Probe localhost for running engines and return the ones that answer."""
    local_host = advertise_host or runtime.detect_local_ip()
    found: list[DetectedEngine] = []
    for label, port, kind in _PROBES:
        if kind == "comfyui":
            if _comfyui_reachable("127.0.0.1", port, timeout):
                host = _reachable_host(local_host, advertise_host, port, kind, timeout)
                found.append(DetectedEngine(label=label, endpoint_url=f"http://{host}:{port}", models=[], media=True))
            continue
        models = _probe("127.0.0.1", port, kind, timeout)
        if models is None:
            continue
        host = _reachable_host(local_host, advertise_host, port, kind, timeout)
        found.append(DetectedEngine(label=label, endpoint_url=f"http://{host}:{port}/v1", models=models))
    return found


def _reachable_host(local_host: str, advertise_host: str | None, port: int, kind: str, timeout: float) -> str:
    """Pick the host to advertise.

    Prefer the local address so other machines can reach the engine, but only if
    the engine is actually bound there — many engines default to loopback. When
    it is not, keep `127.0.0.1` (correct when the grid runs on this same box).
    An explicit --advertise-host is always trusted.
    """
    if advertise_host:
        return local_host
    if local_host == "127.0.0.1":
        return "127.0.0.1"
    reachable = _comfyui_reachable(local_host, port, timeout) if kind == "comfyui" else _probe(local_host, port, kind, timeout) is not None
    return local_host if reachable else "127.0.0.1"


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


def openai_base(url: str, *, timeout: float = 5.0) -> str:
    """The base `grid join --at <url>` serves an engine from: ``<url>/v1`` when the engine serves its OpenAI API
    there, else ``url`` as given.

    llama-server answers ``/models`` and ``/chat/completions`` at its root as well as under ``/v1``, but its root
    ``/completions`` is its NATIVE endpoint: lines no OpenAI client parses, which the relay settled ``completed`` with
    an empty answer, while chat at the same URL warned nobody (grid-platform M2 ticket 23, wave 9, torn-stream F3).
    So ``/v1`` is taken only on a model list there, never appended blindly: a base that is not ``/v1`` (a vendor at
    its root, Gemini's ``/v1beta/openai``) answers otherwise and is kept. While ``/v1`` gives no answer at all, it is
    asked again, so an engine still coming up is not settled at its root on the strength of one bad moment; one that
    never answers keeps the URL as given, as every join did before. The same rule as grid-src's provider runtime
    (``provider/lifecycle._reachable_engine_base``), which also refuses an engine that never answers; a join here
    never has.
    """
    base = url.rstrip("/")
    if base.endswith("/v1"):
        return url
    for attempt in range(_V1_ATTEMPTS):
        if attempt:
            time.sleep(_V1_RETRY_SECONDS)
        serves_v1 = _serves_v1(base, timeout)
        if serves_v1 is not None:
            return f"{base}/v1" if serves_v1 else url
    return url


def _serves_v1(base: str, timeout: float) -> bool | None:
    """Whether ``<base>/v1/models`` answers a model list (``{"data": [...]}``).

    None when there was no answer to read — a transport error or a 5xx — and False on every other answer: httpx
    follows no redirect, so a 3xx says nothing about the route, and a page that is not a model list is not this API
    either. A URL httpx cannot send at all is False too: asking it again would not change it.
    """
    try:
        resp = httpx.get(f"{base}/v1/models", timeout=timeout)
    except (httpx.InvalidURL, httpx.UnsupportedProtocol):
        return False
    except httpx.HTTPError:
        return None
    if resp.status_code >= 500:
        return None
    if not resp.is_success:
        return False
    try:
        listing = resp.json()
    except ValueError:
        return False
    return isinstance(listing, dict) and isinstance(listing.get("data"), list)
