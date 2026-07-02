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


def _props_url(llm_url: str) -> str:
    """llama-server's ``/props`` lives at the server root, not under ``/v1``."""
    base = llm_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return f"{base}/props"


def _probe_props(llm_url: str, timeout: float = _PROPS_TIMEOUT) -> dict[str, bool]:
    """Read ``/props`` to learn what the chat template + model support natively (tools, vision)."""
    resp = _get(_props_url(llm_url), timeout=timeout)
    if resp is None or resp.status_code != 200:
        return {}
    try:
        payload = resp.json()
    except ValueError:
        return {}
    template_caps = payload.get("chat_template_caps") or {}
    modalities = payload.get("modalities") or {}
    if not isinstance(template_caps, dict):
        template_caps = {}
    if not isinstance(modalities, dict):
        modalities = {}
    return {
        "vision": bool(modalities.get("vision")),
        "tools": bool(template_caps.get("supports_tools")),
        "tool_calls": bool(template_caps.get("supports_tool_calls")),
        "parallel_tool_calls": bool(template_caps.get("supports_parallel_tool_calls")),
    }


def _ollama_base(llm_url: str) -> str:
    """Ollama's native API (``/api/...``) lives at the server root, not under ``/v1``."""
    base = llm_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base.rstrip("/")


def _probe_ollama_caps(llm_url: str, model: str, timeout: float = _SHOW_TIMEOUT) -> dict[str, bool]:
    """Read Ollama's declared model capabilities from ``POST /api/show`` (``capabilities: [...]``).

    Authoritative where a live forced-tool probe stays silent: Ollama serves no ``/props``, and its
    OpenAI endpoint may ignore a forced ``tool_choice`` so a small model emits no tool call — yet
    Ollama still *knows* the template supports tools. Best-effort: a non-Ollama engine (llama.cpp,
    vLLM, …) 404s here and we return ``{}``, leaving the live probe + ``/props`` to decide.
    """
    payload = _post_json(f"{_ollama_base(llm_url)}/api/show", {"model": model}, timeout=timeout)
    caps = (payload or {}).get("capabilities")
    if not isinstance(caps, list):
        return {}
    names = {str(c).lower() for c in caps}
    return {"vision": "vision" in names, "tools": "tools" in names}


def _probe_models(llm_url: str, llm_model: str, timeout: float = _PROPS_TIMEOUT) -> set[str]:
    resp = _get(f"{llm_url.rstrip('/')}/models", timeout=timeout)
    if resp is None or resp.status_code != 200:
        return set()
    try:
        payload = resp.json()
    except ValueError:
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


def probe_llama_capabilities(llm_url: str, llm_model: str) -> dict[str, bool]:
    """Live-test which OpenAI capabilities the engine actually honours."""
    base = {"model": llm_model, "stream": False, "max_tokens": 32, "temperature": 0}
    # Structured probes bypass thinking-mode so the response is the raw tool-call / JSON payload.
    structured_base = {**base, "chat_template_kwargs": {"enable_thinking": False}}
    probed = {
        "vision": False,
        "json_object": False,
        "json_schema": False,
        "tools": False,
        "parallel_tool_calls": False,
    }

    props_caps = _probe_props(llm_url)
    ollama_caps = _probe_ollama_caps(llm_url, llm_model)
    probed["vision"] = bool(
        (_probe_models(llm_url, llm_model) & {"image", "multimodal", "vision"})
        or props_caps.get("vision")
        or ollama_caps.get("vision")
    )

    json_object_resp = _post_chat(llm_url, {
        **structured_base,
        "messages": [{"role": "user", "content": 'Return exactly {"ok": true}.'}],
        "response_format": {"type": "json_object"},
    })
    probed["json_object"] = isinstance(
        _probe_json_object(json_object_resp) if isinstance(json_object_resp, dict) else None, dict
    )

    json_schema_resp = _post_chat(llm_url, {
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
    js = _probe_json_object(json_schema_resp) if isinstance(json_schema_resp, dict) else None
    probed["json_schema"] = isinstance(js, dict) and isinstance(js.get("ok"), bool)

    tools_resp = _post_chat(llm_url, {
        **structured_base,
        "max_tokens": 96,
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
    probed["tools"] = bool(
        (isinstance(tools_resp, dict) and _probe_has_tool_calls(tools_resp))
        or (props_caps.get("tools") and props_caps.get("tool_calls"))
        or ollama_caps.get("tools")
    )
    probed["parallel_tool_calls"] = bool(probed["tools"] and props_caps.get("parallel_tool_calls"))
    return probed


DEFAULT_CONTEXT_WINDOW = 128000


def capability_entry(probed: dict[str, bool], context_window: int | None = None) -> dict[str, Any]:
    """Render one model's capability entry (matches the desktop/relay shape). ``context_window`` reflects
    the engine's ``--ctx-size`` when known (the master reads it into the model catalog); it falls back to
    the default when unknown."""
    input_modalities = ["text", "image"] if probed["vision"] else ["text"]
    ctx = int(context_window) if context_window else DEFAULT_CONTEXT_WINDOW
    return {
        "endpoints": ["chat/completions", "completions"],
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


def envelope(model_name: str, probed: dict[str, bool], context_window: int | None = None) -> dict[str, Any]:
    """Wrap a probed-features dict in the ``{schema_version, models}`` envelope the relay requires."""
    return {"schema_version": 1, "models": {model_name: capability_entry(probed, context_window)}}


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
