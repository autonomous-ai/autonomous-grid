"""Probe a running OpenAI-compatible engine for the capabilities a remote node advertises on
register, and benchmark its throughput.

Ported from ``grid-src/grid_cli/provider_runtime/engine/probe.py`` + ``provider/benchmark.py``,
trimmed for the remote serve path: every call goes through ``httpx.Client`` (so it mocks like the
relay / control-plane clients), and a failed probe degrades **silently** to "unsupported" rather
than logging — a node still registers, just with fewer advertised features.

The envelope shape ``{"schema_version": 1, "models": {<name>: <entry>}}`` is mandatory: the relay
silently drops a capabilities map that lacks ``schema_version == 1`` or whose model keys don't
match the advertised list.
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx


_PROBE_TIMEOUT = 15.0
_PROPS_TIMEOUT = 5.0
_SHOW_TIMEOUT = 5.0
_BENCHMARK_TIMEOUT = 60.0

# A minimal valid image for the live vision probe — we only test whether the engine ACCEPTS image
# input, not the answer, so any decodable pixel works. Bump the size if a specific engine rejects
# sub-minimum images (some vision encoders do).
_PROBE_IMAGE_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

# Token budget for the structured inference probes (json/tools). A reasoning model that ignores
# `enable_thinking:False` spends its budget thinking, so a small cap yields an empty completion and the
# probe false-negatives; 512 lets the model still emit the tool_call / JSON afterward. Non-reasoning
# models emit and stop early, so the higher cap costs them nothing.
_STRUCTURED_PROBE_MAX_TOKENS = 512


def capabilities(
    llm_url: str,
    model: str,
    *,
    advertise_as: str | None = None,
    context_window: int | None = None,
) -> dict[str, Any]:
    """One-call public API: live-probe ``llm_url`` for ``model`` and return the register envelope.

    ``advertise_as`` keys the envelope under the *advertised* name when the engine serves ``model``
    under a different alias (``--advertise-as``). The relay drops caps whose model keys don't match
    the advertised list, so an aliased external engine must probe by its real name (what the engine
    answers to) yet register under the alias (what consumers ask for).

    ``context_window`` (the engine's ``--ctx-size``) is advertised so the master can catalog it.
    """
    return envelope(advertise_as or model, probe_llama_capabilities(llm_url, model), context_window)


# --- HTTP (routed through httpx.Client so tests can inject a MockTransport) ---

def _get(url: str, *, timeout: float) -> httpx.Response | None:
    try:
        with httpx.Client(timeout=timeout) as client:
            return client.get(url)
    except httpx.HTTPError:
        return None


def _post_chat(llm_url: str, payload: dict[str, Any], *, timeout: float = _PROBE_TIMEOUT) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{llm_url.rstrip('/')}/chat/completions", json=payload)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _post_json(url: str, payload: dict[str, Any], *, timeout: float) -> dict[str, Any] | None:
    """POST ``payload`` to an arbitrary URL and return the parsed JSON object, or ``None`` on any
    transport / non-200 / non-JSON response (used for Ollama's native ``/api/show``)."""
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --- response parsing ---

def _probe_message(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message") or {}
    return message if isinstance(message, dict) else {}


def _probe_content(payload: dict[str, Any]) -> str:
    content = _probe_message(payload).get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        ]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _probe_json_object(payload: dict[str, Any]) -> dict[str, Any] | None:
    content = _probe_content(payload)
    if not content:
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(content[start:end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _probe_has_tool_calls(payload: dict[str, Any]) -> bool:
    tool_calls = _probe_message(payload).get("tool_calls")
    return isinstance(tool_calls, list) and bool(tool_calls)


def _server_root(llm_url: str) -> str:
    """The engine's server root — native metadata APIs (llama.cpp ``/props``, Ollama ``/api/show``, LM
    Studio ``/api/v0/models``) live at the root, not under the OpenAI ``/v1`` prefix."""
    return llm_url.rstrip("/").removesuffix("/v1").rstrip("/")


def _props_url(llm_url: str) -> str:
    """llama-server's ``/props`` lives at the server root, not under ``/v1``."""
    return f"{_server_root(llm_url)}/props"


def _probe_props(llm_url: str, timeout: float = _PROPS_TIMEOUT) -> dict[str, bool]:
    """Read ``/props`` to learn what the chat template + model support natively (tools, vision)."""
    resp = _get(_props_url(llm_url), timeout=timeout)
    if resp is None or resp.status_code != 200:
        return {}
    try:
        payload = resp.json()
    except ValueError:
        return {}
    if not isinstance(payload, dict):  # a 200 with a non-object body (list/null/str) — never .get() it
        return {}
    template_caps = payload.get("chat_template_caps") or {}
    modalities = payload.get("modalities") or {}
    if not isinstance(template_caps, dict):
        template_caps = {}
    if not isinstance(modalities, dict):
        modalities = {}
    if not template_caps and not modalities:
        # Not a real llama.cpp /props response. A genuine one always carries one of these; LM Studio
        # (and other servers) 200 an unknown path with a bare ``{"error": ...}`` body — returning a
        # non-empty all-False dict here would masquerade as authoritative metadata downstream.
        return {}
    return {
        "vision": bool(modalities.get("vision")),
        "tools": bool(template_caps.get("supports_tools")),
        "tool_calls": bool(template_caps.get("supports_tool_calls")),
        "parallel_tool_calls": bool(template_caps.get("supports_parallel_tool_calls")),
    }


def _probe_ollama_caps(llm_url: str, model: str, timeout: float = _SHOW_TIMEOUT) -> dict[str, bool]:
    """Read Ollama's declared model capabilities from ``POST /api/show`` (``capabilities: [...]``).

    Authoritative where a live forced-tool probe stays silent: Ollama serves no ``/props``, and its
    OpenAI endpoint may ignore a forced ``tool_choice`` so a small model emits no tool call — yet
    Ollama still *knows* the template supports tools. Best-effort: a non-Ollama engine (llama.cpp,
    vLLM, …) 404s here and we return ``{}``, leaving the live probe + ``/props`` to decide.
    """
    payload = _post_json(f"{_server_root(llm_url)}/api/show", {"model": model}, timeout=timeout)
    caps = (payload or {}).get("capabilities")
    if not isinstance(caps, list):
        return {}
    names = {str(c).lower() for c in caps}
    return {"vision": "vision" in names, "tools": "tools" in names}


def _probe_lmstudio_caps(llm_url: str, llm_model: str, timeout: float = _SHOW_TIMEOUT) -> dict[str, bool]:
    """Read LM Studio's native ``GET /api/v0/models`` — one GET, no inference, deterministic (unlike the
    live probes). ``type == "vlm"`` → vision, ``"tool_use" in capabilities`` → tools. Returns ``{}`` for a
    non-LM-Studio engine (404 / connection refused / a ``{"error": ...}`` 200 body with no ``data`` list)
    or a model not in the list, so the caller falls through to /props, /api/show, and the live probes.
    Authoritative where the inference probes false-negative — LM Studio serves GGUF *and* MLX models and
    reports both here, and a reasoning model's forced-tool probe stays silent (it spends its token budget
    thinking) yet LM Studio still declares ``tool_use``. (``max_context_length`` is also reported but not
    consumed yet — the caps envelope's context still comes from ``--ctx-size``.)"""
    resp = _get(f"{_server_root(llm_url)}/api/v0/models", timeout=timeout)
    if resp is None or resp.status_code != 200:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return {}
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return {}
    for entry in entries:
        # Matched by exact id: the probe is called with the engine's real upstream model name (any
        # ``--advertise-as`` alias already resolved), and LM Studio's ``/api/v0/models`` id is that name.
        if isinstance(entry, dict) and entry.get("id") == llm_model:
            entry_type = entry.get("type")
            if not entry_type:
                # Not LM Studio's shape — its /api/v0/models entries always carry a `type`
                # (vlm/llm/embeddings). A bare `{"id": ...}` is a plain /v1/models entry; treat as no
                # metadata so the caller falls through to the live probe rather than reading all-False.
                return {}
            caps = entry.get("capabilities")
            names = {str(c).lower() for c in caps} if isinstance(caps, list) else set()
            return {"vision": entry_type == "vlm", "tools": "tool_use" in names}
    return {}


def _probe_models(llm_url: str, llm_model: str, timeout: float = _PROPS_TIMEOUT) -> set[str]:
    resp = _get(f"{llm_url.rstrip('/')}/models", timeout=timeout)
    if resp is None or resp.status_code != 200:
        return set()
    try:
        payload = resp.json()
    except ValueError:
        return set()
    if not isinstance(payload, dict):  # non-object 200 body — the comprehension below would .get() it
        return set()
    entries = [
        entry
        for key in ("models", "data")
        for entry in payload.get(key, [])
        if isinstance(entry, dict)
    ]
    matched = []
    for entry in entries:
        candidates = [str(entry.get(k) or "") for k in ("id", "name", "model")]
        if any(llm_model == c or llm_model in c for c in candidates):
            matched.append(entry)
    if not matched and len(entries) == 1:
        matched = entries
    caps: set[str] = set()
    for entry in matched:
        raw = entry.get("capabilities") or []
        if isinstance(raw, list):
            caps.update(str(c).lower() for c in raw)
    return caps


def _probe_vision_live(llm_url: str, llm_model: str, timeout: float = _PROBE_TIMEOUT) -> bool:
    """Live-test image input for engines that expose no modality metadata (vLLM, LM Studio, MLX — no
    ``/props``, no ``/api/show``, and a plain ``/v1/models``): send a tiny image and see if the engine
    accepts it. A vision model returns a normal 200 completion; a text-only model rejects the image
    content (4xx) so ``_post_chat`` returns ``None``. Conservative — any non-200, non-object 200 body, or
    transport error → False (better to under-claim vision than route an image job to a text model). A text
    model rejects at input validation, before any forward pass, so this stays cheap for it.

    Limitation: this tests *acceptance* (the engine answered 200 to an image request), not *comprehension*.
    A non-validating engine that accepts the multimodal request but silently ignores the image part — or a
    model that replies "I can't see images" — would false-positive. vLLM and LM Studio (the engines that
    reach this fallback) validate image support and 4xx a text model, so in practice this only bites exotic
    shims; a stricter content check was rejected because it false-negatives real VLMs on a synthetic 1×1
    image. This runs at all only when no modality metadata was found (see the caller's gate)."""
    resp = _post_chat(llm_url, {
        "model": llm_model, "stream": False, "max_tokens": 1, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "."},
            {"type": "image_url", "image_url": {"url": _PROBE_IMAGE_DATA_URI}},
        ]}],
    }, timeout=timeout)
    # ``_post_chat`` returns the raw parsed 200 body, which may not be a dict (list/str/number) — guard
    # like every other ``_post_chat`` caller here, so a malformed body degrades to False, never raises
    # (``_bring_up_engines`` relies on ``probe.capabilities`` never raising).
    return bool(isinstance(resp, dict) and _probe_message(resp))


def _probe_json_object_mode(llm_url: str, structured_base: dict[str, Any]) -> bool:
    """Live-test whether the engine honours ``response_format: {"type": "json_object"}``."""
    resp = _post_chat(llm_url, {
        **structured_base,
        "messages": [{"role": "user", "content": 'Return exactly {"ok": true}.'}],
        "response_format": {"type": "json_object"},
    })
    return isinstance(_probe_json_object(resp) if isinstance(resp, dict) else None, dict)


def _probe_json_schema_mode(llm_url: str, structured_base: dict[str, Any]) -> bool:
    """Live-test whether the engine honours ``response_format: {"type": "json_schema"}``."""
    resp = _post_chat(llm_url, {
        **structured_base,
        "messages": [{"role": "user", "content": "Return a JSON object with ok=true."}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "ProbeResult",
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                    "additionalProperties": False,
                },
            },
        },
    })
    js = _probe_json_object(resp) if isinstance(resp, dict) else None
    return isinstance(js, dict) and isinstance(js.get("ok"), bool)


def _probe_tools_live(llm_url: str, structured_base: dict[str, Any]) -> bool:
    """Live-test tool support by forcing a ``tool_choice`` and checking the reply for a tool_call. The
    flaky fallback used only when no metadata source declares tools — a reasoning model can spend its
    token budget thinking and emit no tool_call even when it supports tools (hence the large budget)."""
    resp = _post_chat(llm_url, {
        **structured_base,
        "messages": [{"role": "user", "content": "Call the echo tool with value ping."}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo a value.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                },
            },
        }],
        "tool_choice": {"type": "function", "function": {"name": "echo"}},
    })
    return bool(isinstance(resp, dict) and _probe_has_tool_calls(resp))


def probe_llama_capabilities(llm_url: str, llm_model: str) -> dict[str, bool]:
    """Live-test which OpenAI capabilities the engine actually honours."""
    base = {"model": llm_model, "stream": False, "max_tokens": _STRUCTURED_PROBE_MAX_TOKENS, "temperature": 0}
    # Structured probes bypass thinking-mode so the response is the raw tool-call / JSON payload. Some
    # engines ignore this kwarg, so the budget above still has to cover a thinking pass (see the constant).
    structured_base = {**base, "chat_template_kwargs": {"enable_thinking": False}}
    probed = {
        "vision": False,
        "json_object": False,
        "json_schema": False,
        "tools": False,
        "parallel_tool_calls": False,
    }

    props_caps = _probe_props(llm_url)              # llama.cpp
    ollama_caps = _probe_ollama_caps(llm_url, llm_model)   # Ollama
    lmstudio_caps = _probe_lmstudio_caps(llm_url, llm_model)  # LM Studio (covers GGUF + MLX served by it)
    model_caps = _probe_models(llm_url, llm_model)  # OpenAI /v1/models `capabilities`, when present
    # Fall back to the live image probe ONLY when NO metadata source answered (bare vLLM / direct
    # mlx_lm.server). props / api/show / api/v0/models are per-model authoritative sources — an answer,
    # even a negative one, is trusted over the live probe (which can false-positive). model_caps (a
    # `/v1/models` `capabilities` list) is deliberately NOT in this gate: it's speculative — most engines
    # omit it and _probe_models falls back to a lone entry — so it only feeds vision via the intersection
    # below, never suppresses the fallback.
    has_modality_metadata = bool(props_caps) or bool(ollama_caps) or bool(lmstudio_caps)
    probed["vision"] = bool(
        (model_caps & {"image", "multimodal", "vision"})
        or props_caps.get("vision")
        or ollama_caps.get("vision")
        or lmstudio_caps.get("vision")
        or (not has_modality_metadata and _probe_vision_live(llm_url, llm_model))
    )

    probed["json_object"] = _probe_json_object_mode(llm_url, structured_base)
    probed["json_schema"] = _probe_json_schema_mode(llm_url, structured_base)

    # Tools: trust a metadata source first; the forced-tool inference probe (flaky on reasoning models)
    # runs only when none answered — ``or`` short-circuits, so it's skipped when metadata already affirms it.
    meta_tools = bool(
        (props_caps.get("tools") and props_caps.get("tool_calls"))
        or ollama_caps.get("tools")
        or lmstudio_caps.get("tools")
    )
    probed["tools"] = bool(meta_tools or _probe_tools_live(llm_url, structured_base))
    probed["parallel_tool_calls"] = bool(probed["tools"] and props_caps.get("parallel_tool_calls"))
    return probed


DEFAULT_CONTEXT_WINDOW = 128000


def capability_entry(
    probed: dict[str, bool], context_window: int | None = None,
    endpoints: list[str] | None = None,
) -> dict[str, Any]:
    """Render one model's capability entry (matches the desktop/relay shape). ``context_window`` reflects
    the engine's ``--ctx-size`` when known (the master reads it into the model catalog); it falls back to
    the default when unknown. ``endpoints`` defaults to the hardware-engine pair; an API engine passes
    ``["chat/completions"]`` — it never serves legacy completions (ADR 0012)."""
    input_modalities = ["text", "image"] if probed["vision"] else ["text"]
    ctx = int(context_window) if context_window else DEFAULT_CONTEXT_WINDOW
    return {
        "endpoints": list(endpoints) if endpoints is not None else ["chat/completions", "completions"],
        "input_modalities": input_modalities,
        "output_modalities": ["text"],
        "context_window": ctx,
        "max_output_tokens": 64000,
        "features": {
            "vision": probed["vision"],
            "tools": probed["tools"],
            "parallel_tool_calls": bool(probed["tools"] and probed.get("parallel_tool_calls")),
            "json_object": probed["json_object"],
            "json_schema": probed["json_schema"],
            "audio": False,
            "logprobs": False,
            "top_logprobs": False,
        },
        "limits": {
            "max_context_tokens": ctx,
            "max_output_tokens": 64000,
            "max_images": 8,
            "max_image_bytes": 4_000_000,
            "max_top_logprobs": 0,
        },
    }


def envelope(
    model_name: str, probed: dict[str, bool], context_window: int | None = None,
    endpoints: list[str] | None = None,
) -> dict[str, Any]:
    """Wrap a probed-features dict in the ``{schema_version, models}`` envelope the relay requires."""
    return {
        "schema_version": 1,
        "models": {model_name: capability_entry(probed, context_window, endpoints=endpoints)},
    }


# --- throughput benchmark ---

def tok_s_from_response(data: dict[str, Any]) -> float | None:
    """Eval tokens/sec from a llama.cpp completion JSON, or ``None`` if absent."""
    if not isinstance(data, dict):
        return None
    timings = data.get("timings")
    if isinstance(timings, dict):
        pps = timings.get("predicted_per_second")
        if isinstance(pps, (int, float)) and pps > 0:
            return float(pps)
    return None


def benchmark_tok_s(llm_url: str, model: str, *, timeout: float = _BENCHMARK_TIMEOUT) -> float | None:
    """Run one short completion against ``llm_url`` and return eval tokens/sec, or ``None``.

    Prefers llama.cpp's ``timings.predicted_per_second``; falls back to wall-clock over
    ``usage.completion_tokens`` for other engines. ``None`` on any failure (e.g. a media-only node).
    """
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with a short greeting."}],
        "max_tokens": 64,
        "stream": False,
        "temperature": 0.0,
    }
    started = time.monotonic()
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(f"{llm_url.rstrip('/')}/chat/completions", json=body)
    except httpx.HTTPError:
        return None
    elapsed = time.monotonic() - started
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None

    pps = tok_s_from_response(data)
    if pps:
        return round(pps, 1)
    usage = data.get("usage") if isinstance(data, dict) else None
    completion_tokens = (usage or {}).get("completion_tokens") or 0
    if completion_tokens and elapsed > 0:
        return round(completion_tokens / elapsed, 1)
    return None
