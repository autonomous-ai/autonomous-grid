"""Serve a locally-installed coding CLI as an OpenAI-compatible engine.

One subprocess per request; the CLI's own sign-in is the credential — the grid never holds one.
This file is the shared pipeline and knows about no CLI in particular. Each CLI's differences live
in `shared/agent/seats/<name>.py` as two functions:

    invoke(binary, prepared, tmpdir) -> (argv, stdin)
    decode(proc, tmpdir)             -> SeatResult

Models come from `api_catalog.WHITELISTS[kind].entries`, never a second list here.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from types import SimpleNamespace

from shared import paths
from shared.models import api_catalog


class SeatError(RuntimeError):
    """The seat could not answer — binary missing, not signed in, timeout, or a CLI failure."""


class SeatBadRequest(SeatError):
    """The caller is wrong (unknown model, no messages) — the HTTP layer answers 400, not 502."""


@dataclass(frozen=True)
class SeatResult:
    """One decoded CLI run, in the shape every adapter must produce."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    num_turns: int = 0
    session_id: str = ""


@dataclass(frozen=True)
class Reasoning:
    """One chunk of the model's thinking, travelling the same channel as an answer delta.

    A marker type rather than a second callback: a seat emits thinking and answer text on one
    ordered stream, and splitting them into two channels would lose which came first. It must not be
    a bare string — thinking is NOT part of the answer, so it never reaches `_SafeText`, never counts
    toward the text the parse reads back, and never lands in `content`.
    """

    text: str


@dataclass(frozen=True)
class TurnEnd:
    """One of the CLI's turns finished — NOT the request.

    A CLI handed a replayed transcript may run several turns for one request, answering each past
    question again on its way to the live one. Only the last of those is the answer; the rest are
    re-runs and must not reach the client. `parse_event` marks the boundaries with this and
    `stream_seat` counts them, because only the seat knows how many turns it asked for.
    """


@dataclass(frozen=True)
class QuotaSnapshot:
    """One quota reading. Percentages are 0-100; resets stay verbatim (they carry no year)."""

    session_pct: int
    week_pct: int = 0
    session_reset: str = ""
    week_reset: str = ""


@dataclass(frozen=True)
class SeatSpec:
    """Everything that differs between one CLI seat and another. Add a row, not a branch."""

    kind: str            # catalog kind; also the advertised model namespace
    binary: str          # executable name looked up on PATH
    label: str           # human name used in messages
    signin_argv: tuple   # argv after the binary that exits 0 iff signed in
    invoke: object       # (binary, prepared, tmpdir) -> (argv, stdin)
    decode: object       # (proc, tmpdir) -> SeatResult
    login_argv: tuple = ()          # argv that starts an interactive sign-in
    quota_argv: tuple = ()          # argv printing a usage screen; () = this CLI has no quota API
    read_quota: object = None       # (binary) -> QuotaSnapshot | None, for a CLI whose quota is
                                    # not a printed screen. Wins over quota_argv when set.
    tool_protocol: object = None    # which ToolProtocol this seat's model reads best. None = OPENAI.
    run: object = None              # (binary, prepared, timeout, on_delta) -> SeatResult, for a CLI
                                    # driven over a protocol rather than one subprocess per request.
                                    # `on_delta(text)` is called per streamed chunk, or None.
                                    # Wins over invoke/decode when set.
    parse_usage: object = None      # (text) -> QuotaSnapshot | None
    parse_event: object = None      # (event) -> str | Reasoning | QuotaSnapshot | TurnEnd | None
    expected_turns: object = None   # (stdin) -> how many turns this input will make the CLI run.
                                    # None = one, the ordinary case. Set only by a CLI whose input
                                    # format re-runs replayed history (see seats/claude.py).
    home_env: str = ""              # env var pointing the CLI at its own home; "" = shares the user's
    config_files: tuple = ()        # ((relative path, text), …) written into that home
    tool_note: str = ""             # extra instruction appended to the tool block, for a CLI whose
                                    # own tools stay visible to the model and must be named to be
                                    # ruled out. "" for a CLI that can hide them (claude's --tools "")


@dataclass(frozen=True)
class SeatOptions:
    """Per-join settings, one object end to end: flags -> record -> child argv -> server."""

    port: int = 8099
    timeout: float = 600.0
    concurrency: int = 1
    session_limit: int = None
    week_limit: int = None
    quota_ttl: float = 60.0


def option_names():
    return tuple(f.name for f in fields(SeatOptions))


def options_from_args(args, default_port=None):
    """Read the `--seat-*` flags; an unset flag falls back to this seat's own default."""
    defaults = SeatOptions(port=default_port) if default_port else SeatOptions()
    values = {}
    for name in option_names():
        supplied = getattr(args, f"seat_{name}", None)
        values[name] = supplied if supplied is not None else getattr(defaults, name)
    return SeatOptions(**values)


def options_from_spec(spec):
    stored = spec.get("seat") or {}
    return SeatOptions(**{k: v for k, v in stored.items() if k in option_names()})


def options_to_dict(options):
    return asdict(options)


def options_child_argv(options, kind):
    """The `__cli-seat-server` argv these options imply."""
    argv = ["--kind", kind, "--port", str(options.port), "--timeout", str(options.timeout),
            "--concurrency", str(options.concurrency), "--quota-ttl", str(options.quota_ttl)]
    if options.session_limit is not None:
        argv += ["--session-limit", str(options.session_limit)]
    if options.week_limit is not None:
        argv += ["--week-limit", str(options.week_limit)]
    return argv


# the tool protocol, shared by every seat

@dataclass(frozen=True)
class ToolProtocol:
    """How a model is asked to emit tool calls as text, and how that text is read back.

    `render(tools)` turns OpenAI `tools[]` into instruction text. The answer comes back as text and
    the caller parses it, so a seat never needs to know the format going the other way.
    """

    name: str
    render: object
    parse: object = None


# Injected ONLY when the caller sends native `tools[]`. A caller that describes its own tool
# convention in a system message gets passthrough — two copies would give the model rival
# protocols. The shape asked for is the OpenAI one the model already knows from that wire.
OPENAI_TOOL_TEMPLATE = """These tools are available to you, as OpenAI function definitions:
{tools_json}

To use one, reply with a JSON object in exactly this shape and nothing else:
{{"tool_calls": [{{"type": "function", "function": {{"name": <tool-name>, \
"arguments": {{<arguments-object>}}}}}}]}}

Emit one entry per call; several calls may go in the same array. Reply with plain text instead \
whenever no tool is needed."""



# binary + sign-in

def seat_home(spec):
    """The seat's private home, or None when this CLI has no such notion."""
    if not spec.home_env:
        return None
    return paths.seat_home(spec.kind)


def ensure_home(spec):
    """Create the seat's home and write its config. Returns the env the CLI must run with.

    Rewritten every start rather than only when missing: the config IS the lockdown, so a
    hand-edited or stale file would silently loosen it.
    """
    home = seat_home(spec)
    if home is None:
        return dict(os.environ)
    home.mkdir(parents=True, exist_ok=True)
    for relative, text in spec.config_files:
        target = home / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return {**os.environ, spec.home_env: str(home)}


def bin_env(spec):
    """The env var that pins this seat's binary, e.g. GRID_CLAUDE_BIN. Derived, so a new seat
    gets its override for free and cannot spell it inconsistently."""
    return "GRID_" + spec.kind.replace("-", "_").upper() + "_BIN"


def seat_bin(spec):
    pinned = os.environ.get(bin_env(spec), "").strip()
    if pinned:
        return pinned if os.path.isfile(pinned) and os.access(pinned, os.X_OK) else None
    return shutil.which(spec.binary)


def is_signed_in(spec, binary):
    """Reads the exit code, never the wording — the words are the vendor's to change."""
    try:
        proc = subprocess.run([binary, *spec.signin_argv], capture_output=True, text=True,
                              timeout=30, env=ensure_home(spec))
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def assert_available(spec):
    """The binary path, or an error. Checks sign-in too: a signed-out seat registers looking
    healthy and then fails every request into a log nobody tails."""
    binary = seat_bin(spec)
    if binary is None:
        raise SeatError(
            f"The `{spec.binary}` CLI was not found on PATH. Install {spec.label}, or point at a "
            f"build with {bin_env(spec)}=/path/to/{spec.binary}."
        )
    if not is_signed_in(spec, binary):
        home = seat_home(spec)
        where = f"{spec.home_env}={home} " if home else ""
        raise SeatError(
            f"This seat is not signed in. Run "
            f"`{where}{spec.binary} {' '.join(spec.login_argv or ('login',))}` and join again.\n"
            f"The seat keeps its own sign-in" + (f" in {home}" if home else "") +
            ", so your personal one is never copied or disturbed."
        )
    return binary


def advertised_models(kind):
    """What this seat serves, from the ONE catalog row the join also validates against."""
    return [api_catalog.advertised_name(kind, entry) for entry in api_catalog.entries_for(kind)]


def alias_for(kind, advertised):
    """`claude:sonnet` -> `sonnet`. Bare names resolve too — the relay rewrites to the upstream
    spelling before forwarding, so the seat receives both forms."""
    name = (advertised or "").strip()
    entry = api_catalog.find_advertised(kind, name)
    if entry is not None:
        return entry.vendor_name
    if any(e.vendor_name == name for e in api_catalog.resolvable_entries(kind)):
        return name
    return None


# quota

def probe_quota(spec, binary, timeout=30.0):
    """Current quota, or None when unreadable. Never raises — unknown means keep serving."""
    if spec.read_quota is not None:
        try:
            return spec.read_quota(binary)
        except Exception:  # noqa: BLE001 — a quota probe must never break serving
            return None
    if not spec.quota_argv or spec.parse_usage is None:
        return None
    try:
        proc = subprocess.run(
            [binary, *spec.quota_argv], capture_output=True, text=True, timeout=timeout,
            env=ensure_home(spec)
        )
        envelope = json.loads((proc.stdout or "").strip())
    except (OSError, subprocess.SubprocessError, TypeError, ValueError):
        return None
    if not isinstance(envelope, dict) or envelope.get("is_error"):
        return None
    return spec.parse_usage(str(envelope.get("result") or ""))


def quota_headroom_pct(snapshot, options):
    """How much allowance is left, 0-100. Unknown reads as 100 (full).

    Measured against the operator's OWN ceiling when they set one, because that is the point the
    seat stops serving — a seat capped at 50% is empty at 50% used, not at 100%.
    """
    if snapshot is None:
        return 100.0
    remaining = []
    for pct, limit in ((snapshot.session_pct, options.session_limit),
                       (snapshot.week_pct, options.week_limit)):
        ceiling = limit if limit is not None else 100
        remaining.append(max(0.0, (ceiling - pct) / ceiling * 100.0) if ceiling else 0.0)
    return min(remaining) if remaining else 100.0


def quota_refusal(snapshot, options):
    """Refusal message when a ceiling is breached, else None. Unknown never refuses (fail-open).
    Weekly is checked first — a spent week costs days, a spent session hours."""
    if snapshot is None:
        return None
    windows = (
        (snapshot.week_pct, options.week_limit, "weekly", snapshot.week_reset),
        (snapshot.session_pct, options.session_limit, "session", snapshot.session_reset),
    )
    for pct, limit, window, reset in windows:
        if limit is not None and pct >= limit:
            return (
                f"Seat is out of allowance: {window} usage {pct}% has reached the {limit}% "
                f"ceiling set for this seat" + (f"; resets {reset}" if reset else "")
            )
    return None


# request -> prompt

def _flatten_content(content):
    """Text and replayed reasoning survive; anything else leaves a visible marker rather than
    vanishing without trace.

    A `thinking` block is the model's own prior reasoning, replayed by the client on a later
    turn — `{"type": "thinking", "thinking": "<reasoning text>", "signature": "<opaque>"}`. Folded
    into the prompt the same way ordinary text is, in the same position, so the model keeps its
    own chain of thought past turn one. `signature` is dropped: it only authenticates a reasoning
    block on the way back out to Anthropic, and this seat can never produce a valid one for its
    own answers.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text":
                chunks.append(str(part.get("text") or ""))
            elif kind == "thinking":
                chunks.append(str(part.get("thinking") or ""))
            elif kind in ("image_url", "input_image"):
                chunks.append("[image omitted — this engine serves text only]")
            elif kind in ("tool_use", "tool_result"):
                continue  # build_prompt renders these separately — skip, don't mark "omitted"
            else:
                # Previously dropped with no trace at all. A short, neutral marker makes the
                # next unhandled block shape discoverable instead of silently invisible.
                chunks.append(f"[{kind or 'unknown'} block omitted — this engine serves text only]")
        return "\n".join(c for c in chunks if c)
    return "" if content is None else str(content)


# Used when the caller sends no system message and no tools. It must not be empty: the seats
# REPLACE the vendor's own prompt, and an empty replacement either errors (codex refuses an empty
# instructions file) or falls back to the vendor prompt — which is what leaks the provider's paths.
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."

# Appended to the tool block. Measured necessity, not belt-and-braces: without it a codex seat
# answered "I couldn't write index.html because the workspace denied the file-edit request" instead
# of emitting a tool call — it believed it still had an editor and reported the refusal as the
# outcome. It only works when the vendor's base prompt is REPLACED too; left in place, that prompt
# reasserts the agent identity and the model goes back to trying the work itself.
#
# Do not soften this for a seat it does not fit. It was reworded once, to stop asserting "no file,
# shell, editor or network tool" on codex, where the model can see `exec` in its own list — and the
# rewording measurably cost claude, where the sentence is exactly true (`--tools ""`) and the
# enumeration is the part that works. A seat whose CLI leaves its own tools visible corrects this in
# its `tool_note` instead, which is appended after and is the last word.
TOOL_DELEGATION_NOTE = (
    "You have NO ability to execute anything yourself — no file, shell, editor or network tool. "
    "Any workspace, sandbox or approval described to you concerns the process you run in, NOT the "
    "user's request; ignore it entirely and never report that an operation was denied. Your only "
    "way to act is to emit a tool call; the caller executes it and returns the result to you."
)


def render_openai_tools(tools):
    """OpenAI `tools[]` -> the instruction text, with the delegation note appended."""
    block = OPENAI_TOOL_TEMPLATE.format(tools_json=json.dumps(tools, ensure_ascii=False))
    return f"{block}\n\n{TOOL_DELEGATION_NOTE}"


def _json_objects(text):
    """Every JSON object in `text`, as `(start, end, value)`.

    Scans with the real JSON decoder rather than a pattern: the payload nests braces and quotes
    them inside strings, so a `\\{.*?\\}` or a brace counter truncates any call whose argument is
    an object — the common case.
    """
    # strict=False: a model writing a shell command puts REAL newlines and tabs inside the JSON
    # string. Strict mode rejects those as control characters, the scan finds no call, and the raw
    # `{"tool_calls": …}` is handed back as prose — the user sees it printed instead of run.
    decoder = json.JSONDecoder(strict=False)
    index = 0
    while True:
        index = text.find("{", index)
        if index < 0:
            return
        try:
            value, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        yield index, end, value
        index = end


def _one_call(entry):
    """One element of `tool_calls[]`, normalised — or None if it is not a call.

    `arguments` is emitted as a STRING because that is what the consumer expects: the relay calls
    `json.loads` on it to build the Anthropic `tool_use.input`. Accepted as either an object (what
    a model naturally writes) or an already-encoded string (what the OpenAI wire actually carries).
    """
    if not isinstance(entry, dict):
        return None
    function = entry.get("function") if isinstance(entry.get("function"), dict) else entry
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments, strict=False)
        except ValueError:
            arguments = {}
    return {
        "id": str(entry.get("id") or f"call_{uuid.uuid4().hex[:24]}"),
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments if isinstance(arguments, dict) else {},
                                    ensure_ascii=False),
        },
    }


def parse_openai_tool_calls(text):
    """Answer text -> `(prose, tool_calls)`, ready for the relay to translate.

    The model is asked for the shape it already knows from the OpenAI wire, so this looks for one
    thing only: a JSON object carrying `tool_calls`. Everything outside it is prose, kept.

    An object that carries no usable call is LEFT WHERE IT IS, deliberately: a half-written call is
    a result worth seeing, and dropping it turns a visible malformation into a model that
    mysteriously did nothing.
    """
    calls, prose_parts, cursor = [], [], 0
    for start, end, value in _json_objects(text):
        if not isinstance(value, dict) or not isinstance(value.get("tool_calls"), list):
            continue
        found = [call for call in (_one_call(c) for c in value["tool_calls"]) if call]
        if not found:
            continue
        prose_parts.append(text[cursor:start])
        cursor = end
        calls.extend(found)
    if not calls:
        return text, []
    prose_parts.append(text[cursor:])
    return "".join(prose_parts).strip(), calls


# The same shape OPENAI_TOOL_TEMPLATE asks for, as a schema a CLI can enforce at decode time.
# Asking produces a call that is *usually* well-formed; measured on a live codex turn, one came back
# with a closing brace missing and the whole object was printed to the user as prose. A constrained
# decode cannot emit that. `text` stays so an ordinary answer is still expressible — constrain the
# shape, not the choice of whether to call a tool.
#
# Strict structured outputs, whose rules the first draft broke and the vendor named exactly:
# "'additionalProperties' is required to be supplied and to be false". Every object must also list
# EVERY property in `required` — there are no optional keys — so `tool_calls` is required and simply
# empty when no tool is wanted. `arguments` is a STRING holding JSON, not an object: a free-form
# object cannot be expressed under these rules, and the OpenAI wire spells it that way regardless
# (`_one_call` already decodes both).
OPENAI_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "tool_calls"],
    "properties": {
        "text": {"type": "string",
                 "description": "The reply. Empty when a tool call is the whole answer."},
        "tool_calls": {
            "type": "array",
            "description": "Empty when no tool is needed.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "arguments"],
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "string",
                                  "description": "JSON object, encoded as a string."},
                },
            },
        },
    },
}


def parse_structured_answer(text):
    """A schema-constrained answer -> `(prose, calls)`. Falls back to the text scan.

    The fallback is not belt-and-braces: `outputSchema` binds the FINAL message only, so a turn that
    ends in prose, or a CLI that ignored the schema, still arrives in the old shape.
    """
    try:
        value = json.loads(text, strict=False)
    except (TypeError, ValueError):
        return parse_openai_tool_calls(text)
    if not isinstance(value, dict) or "text" not in value:
        return parse_openai_tool_calls(text)
    calls = [c for c in (_one_call(c) for c in value.get("tool_calls") or []) if c]
    return str(value.get("text") or ""), calls


OPENAI = ToolProtocol(name="openai", render=render_openai_tools, parse=parse_openai_tool_calls)
STRUCTURED = ToolProtocol(name="structured", render=render_openai_tools,
                          parse=parse_structured_answer)


# Anthropic's own shape: one `tool_use` block instead of an array of `tool_calls`. Kept as a
# second, independent protocol rather than a translation of OPENAI's — a seat serving `/messages`
# natively should never need a round trip through the OpenAI shape to get there.
ANTHROPIC_TOOL_TEMPLATE = """These tools are available to you:
{tools_json}

To use one, reply with a JSON object in exactly this shape and nothing else:
{{"type": "tool_use", "name": <tool-name>, "input": {{<input-object>}}}}

Emit one object per call. Reply with plain text instead whenever no tool is needed."""


def render_anthropic_tools(tools):
    block = ANTHROPIC_TOOL_TEMPLATE.format(tools_json=json.dumps(tools, ensure_ascii=False))
    return f"{block}\n\n{TOOL_DELEGATION_NOTE}"


def parse_anthropic_tool_uses(text):
    """Answer text -> `(prose, tool_use blocks)`. Same scan as the OpenAI parse; only the keys
    differ. An object that is not a usable call is LEFT WHERE IT IS — a half-written call is a
    result worth seeing, and dropping it turns a visible malformation into a model that
    mysteriously did nothing."""
    calls, prose_parts, cursor = [], [], 0
    for start, end, value in _json_objects(text):
        if not isinstance(value, dict) or value.get("type") != "tool_use":
            continue
        name = value.get("name")
        if not isinstance(name, str) or not name:
            continue
        prose_parts.append(text[cursor:start])
        cursor = end
        payload = value.get("input")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload, strict=False)
            except ValueError:
                payload = {}
        calls.append({"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:24]}",
                      "name": name, "input": payload if isinstance(payload, dict) else {}})
    if not calls:
        return text, []
    prose_parts.append(text[cursor:])
    return "".join(prose_parts).strip(), calls


ANTHROPIC = ToolProtocol(name="anthropic", render=render_anthropic_tools,
                         parse=parse_anthropic_tool_uses)


def build_system_prompt(messages, tools=None, protocol=None):
    """The caller's system messages, verbatim.

    The tool block does NOT go here. Measured against Claude Code's real 27 KB system prompt: with
    the block appended to it, the model answered `"I don't have access to a Read tool"` — a system
    prompt describing a harness where tools are native out-argues an instruction buried at its end.
    Moved to the end of the turn (`build_prompt`), which is the last thing the model reads, it
    emitted a tool call. `tools`/`protocol` stay in the signature so existing callers still work.
    """
    parts = [_flatten_content(m.get("content")) for m in messages if m.get("role") == "system"]
    system = "\n\n".join(p for p in parts if p.strip())
    return system if system.strip() else DEFAULT_SYSTEM_PROMPT


def _has_tool_result(content):
    """True iff `content` is an Anthropic block list carrying a `tool_result` — the lone-user-turn
    shortcut below must not take this path, or the result vanishes with no `User:`/`Tool result:`
    line to carry it."""
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )


def build_prompt(messages):
    """The non-system messages as one stdin prompt. Each request is a fresh process with no
    memory, so the whole transcript is replayed. A lone user turn passes through verbatim."""
    turns = [m for m in messages if m.get("role") != "system"]
    if not turns:
        raise SeatBadRequest("messages[] must contain at least one non-system message.")
    if (len(turns) == 1 and turns[0].get("role") == "user"
            and not _has_tool_result(turns[0].get("content"))):
        return _flatten_content(turns[0].get("content"))

    lines = []
    for message in turns:
        role = message.get("role")
        content = message.get("content")
        text = _flatten_content(content)
        if role == "user":
            # Anthropic's own shape: a tool result arrives as a `tool_result` block INSIDE a user
            # message, never as a `role: "tool"` message. Rendered with the IDENTICAL wording the
            # OpenAI `role: "tool"` branch below uses — one convention, not two — rather than
            # "User: Tool result: …", which would be a second spelling of the same thing.
            results = [b for b in (content if isinstance(content, list) else [])
                       if isinstance(b, dict) and b.get("type") == "tool_result"]
            if results:
                for block in results:
                    lines.append(f"Tool result: {_flatten_content(block.get('content'))}")
                rest = _flatten_content([b for b in content if not
                                         (isinstance(b, dict) and b.get("type") == "tool_result")])
                if rest:
                    lines.append(f"User: {rest}")
            else:
                lines.append(f"User: {text}" if text else "User:")
        elif role == "assistant":
            rendered = [f"Assistant: {text}" if text else "Assistant:"]
            # Replayed in the SAME shape the instruction asks for. They used to be replayed as
            # Hermes `<tool_call>` tags while the instruction asked for this JSON, so from the
            # second turn on the model saw one convention in the history and was told another.
            calls = []
            for call in message.get("tool_calls") or []:
                function = (call or {}).get("function") or {}
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except (TypeError, ValueError):
                        pass  # keep the raw string rather than lose the call
                calls.append({"type": "function",
                              "function": {"name": function.get("name"), "arguments": arguments}})
            if calls:
                rendered.append(json.dumps({"tool_calls": calls}, ensure_ascii=False))
            # Anthropic's own shape: a `tool_use` block sits IN the content list rather than in a
            # separate `tool_calls` field. Replayed one JSON object per call — the same convention
            # ANTHROPIC_TOOL_TEMPLATE asks the model to write — so the history and the instruction
            # agree from the second turn on, same as the OpenAI case above.
            for block in (content if isinstance(content, list) else []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    rendered.append(json.dumps({
                        "type": "tool_use",
                        "name": block.get("name"),
                        "input": block.get("input") if isinstance(block.get("input"), dict) else {},
                    }, ensure_ascii=False))
            lines.append("\n".join(rendered))
        elif role == "tool":
            lines.append(f"Tool result: {text}")
        else:
            lines.append(text)
    return "\n\n".join(line for line in lines if line)


# Caller-side request metadata, not content: `cache_control` belongs to the caller's own API call
# and 400s when forwarded into ours.
STRIPPED_KEYS = ("cache_control",)

# Framed, because the payload is a JSON array and without this the model describes it instead of
# continuing it.
JSON_PROMPT_HEADER = (
    "The conversation so far, as JSON. Continue it: answer the last message. "
    "Do not describe or repeat the JSON."
)


def _strip_metadata(value):
    if isinstance(value, dict):
        return {k: _strip_metadata(v) for k, v in value.items() if k not in STRIPPED_KEYS}
    if isinstance(value, list):
        return [_strip_metadata(v) for v in value]
    return value


def json_prompt(messages, tools_text=None, keep_system=False):
    """The transcript as verbatim JSON — nothing flattened into prose.

    A `tool_use` stays a tool_use and a `tool_result` stays a tool_result, so the model can see the
    shape it is expected to answer in. Flattening turned both into sentences, and the model then had
    to guess the format back.

    `keep_system` keeps `role: "system"` messages in the transcript, for a wire whose system prompt
    came from a top-level field and so never read them.
    """
    turns = _strip_metadata(
        list(messages) if keep_system else [m for m in messages if m.get("role") != "system"])
    if not turns:
        raise SeatBadRequest("messages[] must contain at least one non-system message.")
    body = json.dumps(turns, ensure_ascii=False, indent=2, default=str)
    prompt = f"{JSON_PROMPT_HEADER}\n\n{body}"
    return f"{prompt}\n\n{tools_text}" if tools_text else prompt


def _responses_content_parts(content):
    """Normalise Responses-API content parts so the existing flattener reads them.

    The Responses dialect spells text parts ``input_text`` / ``output_text`` where the chat wire
    uses plain ``text``. This rewrites just those type tags and leaves everything else (including
    a bare string) untouched, so ``_flatten_content`` — shared across all three wires — needs no
    dialect branch of its own.
    """
    if not isinstance(content, list):
        return content
    normalised = []
    for part in content:
        if isinstance(part, dict) and part.get("type") in ("input_text", "output_text"):
            normalised.append({"type": "text", "text": part.get("text", "")})
        else:
            normalised.append(part)
    return normalised


def _messages_from_responses_input(body):
    """Responses API ``input`` -> chat ``messages[]`` the shared prompt builder consumes.

    The ``input`` field is the Responses analogue of ``messages``: a plain string (a lone user
    turn) or an array of items. Message items use the flat ``{role, content}`` shape; output items
    wrap it under ``{type: "message", message: {…}}``. Function-call items (``function_call`` /
    ``function_call_output``) are mapped to the assistant-tool-call / tool-result shapes the
    ``build_prompt`` replayer already reads, so a multi-turn tool conversation round-trips through
    the Responses wire exactly as it does through chat.
    """
    raw = body.get("input")
    if isinstance(raw, str):
        return [{"role": "user", "content": raw}]
    if not isinstance(raw, list):
        return []
    messages = []
    for item in raw:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "message":
            inner = item.get("message") if isinstance(item.get("message"), dict) else item
            messages.append({
                "role": inner.get("role") or "assistant",
                "content": _responses_content_parts(inner.get("content")),
            })
        elif itype == "function_call":
            messages.append({
                "role": "assistant",
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }],
            })
        elif itype == "function_call_output":
            messages.append({"role": "tool", "content": str(item.get("output") or "")})
        elif "role" in item:
            messages.append({
                "role": item.get("role") or "user",
                "content": _responses_content_parts(item.get("content")),
            })
    return messages


@dataclass(frozen=True)
class PreparedRequest:
    """Built once per request — the server logs it too, and recomputing would re-serialise the
    whole tool schema on the event loop."""

    model: str
    model_alias: str
    prompt: str
    system_prompt: str
    # The protocol whose instruction went INTO the prompt, so the same one reads the answer back —
    # None when the caller sent no `tools[]`, which is what keeps a client that brought its own
    # convention (Hermes' own `<tools>` system message) getting its text back verbatim.
    tool_protocol: object = None
    # Which wire the caller spoke — "openai", "anthropic", or "responses". Carried on the request
    # so the server answers in the SAME wire it was asked in without re-deriving it from the body
    # a second time.
    wire: str = "openai"
    # The seat's own effort level, derived from the request's `thinking` field, the Responses
    # `reasoning.effort` field, or the chat `reasoning_effort` field — "" when the caller asked for
    # none, so today's argv/turn params are unaffected.
    effort: str = ""
    messages: tuple = ()   # raw non-system messages, for structured-input seats
    tool_text: str = ""    # rendered tool protocol text, "" when caller sent no tools


def _effort_for_thinking(thinking):
    """The request's Anthropic `thinking` field -> a `claude --effort` level, or "" for none.

    The wire carries a token budget (`{"type": "enabled", "budget_tokens": N}`); `claude` only
    exposes five coarse levels (low, medium, high, xhigh, max) with no token knob, so this mapping
    is a judgement call, not a fact. Anthropic's own extended-thinking docs use 1024 tokens as the
    documented minimum and ~10000 as the typical worked example, so the thresholds below put a
    bare-minimum budget at `low`, put the common ~10000 case at `medium`, and reserve `xhigh`/`max`
    for callers who explicitly asked for a large budget (the range where deeper multi-step
    reasoning is typically wanted).

    A live, unmodified Claude Code run sends neither of the two values this function used to know:
    it sends `{"type": "adaptive", "display": "omitted"}` — the model decides its own budget, no
    number to map. Treating that as "no effort" (its old behaviour) silently downgraded the CLI's
    own default request shape: thinking is on, the turn is billed, and `claude` is never told to
    think. There is no budget to derive a rung from, so `adaptive` is pinned to `"medium"` — a
    judgement call, not a computed value, but a deliberate one: it is the same middle rung an
    `enabled` request with no usable `budget_tokens` now also gets (below), so "the caller turned
    thinking on but gave no number" reads the same way regardless of which of the two shapes for
    saying that arrives.

    Every OTHER type value — a future one none of us has seen yet, or a typo — is treated the same
    way rather than falling through to "no effort": the client sent a non-empty `type` that is not
    the explicit `"disabled"` off-switch, so it asked for *something*, and silence is the exact
    failure mode this function exists to close. Only a genuinely absent/malformed field (no dict,
    no `type`, or `type: "disabled"`) returns "" — that is the one case where nothing was asked for,
    so an unset request still passes no flag at all, today's behaviour for that case unchanged.
    """
    if not isinstance(thinking, dict):
        return ""
    kind = thinking.get("type")
    if not kind or kind == "disabled":
        return ""
    budget = thinking.get("budget_tokens")
    if kind == "enabled" and isinstance(budget, (int, float)) and not isinstance(budget, bool) \
            and budget > 0:
        if budget < 4000:
            return "low"
        if budget < 10000:
            return "medium"
        if budget < 32000:
            return "high"
        if budget < 60000:
            return "xhigh"
        return "max"
    # "enabled" with no usable budget, "adaptive", or any unrecognised type: all mean the caller
    # turned thinking on without handing us a number to grade. See the docstring above for why
    # "medium" and not "".
    return "medium"


def _effort_for_reasoning_effort(value):
    """The OpenAI standard `reasoning_effort` field -> a CLI effort level, or "" for none.

    Passed straight through 1:1 (`low`/`medium`/`high`) rather than climbed onto a higher rung,
    even though both CLIs accept a wider ladder (claude adds xhigh/max; codex adds
    none/minimal/xhigh/ultra). Unlike `thinking.budget_tokens`, this field is not a measurement a
    mapping has to interpret — `low`/`medium`/`high` IS OpenAI's own vocabulary for how hard the
    model should think, and both CLIs use the identical words for the identical idea. A caller who
    asked for "high" asked for OpenAI's high; assuming they meant the CLI's most extreme setting
    would be inventing intent the wire never carried. Anything outside those three words (or a
    missing field) reads as no effort at all, the same as an absent `thinking`.
    """
    level = str(value or "").strip().lower()
    return level if level in ("low", "medium", "high") else ""


def _resolve_effort(body):
    """The request body's reasoning-effort signal -> a CLI effort level, or "" for none.

    Three fields can carry this, never more than one meaningfully at once since they belong to
    different wires: Anthropic's own `thinking` (a token budget) reaches the seat when a client
    speaks `/messages` natively; the Responses API's `reasoning.effort` reaches it when a client
    speaks `/responses` natively; the OpenAI Chat standard `reasoning_effort` reaches it once an
    engine that speaks neither has translated the request to the chat wire first. `thinking` wins
    whenever it actually resolves to an effort — it is the richer signal, carrying a real budget
    rather than one of three words. A request naming none of the three returns "", so an ordinary
    body's resulting argv/turn params are unaffected.
    """
    thinking = body.get("thinking")
    budget = thinking.get("budget_tokens") if isinstance(thinking, dict) else None
    if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0:
        return _effort_for_thinking(thinking)   # a real number beats every word below

    # A word the caller actually wrote beats one this file infers. Claude Code sends BOTH
    # `thinking: {"type": "adaptive"}` (no number, so we guess "medium") and
    # `output_config: {"effort": "high"}` — captured live. Reading only `thinking` graded its
    # high-effort request as medium.
    for source in (body.get("output_config"), body.get("reasoning")):
        if isinstance(source, dict):
            effort = _effort_for_reasoning_effort(source.get("effort"))
            if effort:
                return effort
    return _effort_for_reasoning_effort(body.get("reasoning_effort")) or _effort_for_thinking(thinking)


def _reject_server_tools(tools):
    """A tool with a `type` is one Anthropic runs on its own servers (`web_search`, `bash`,
    `code_execution`…), not one the caller executes. This seat has no such server, so accepting
    one would emit a tool call nobody answers — the consumer just hangs."""
    for tool in tools or []:
        kind = tool.get("type") if isinstance(tool, dict) else None
        if kind and kind != "function":
            raise SeatBadRequest(
                f"Unsupported tool type: {kind} ({tool.get('name', '?')}). "
                "This seat executes nothing itself; the caller runs every tool."
            )


def _tool_name(tool):
    """The name a tool can be called by, or "" when it has none."""
    if not isinstance(tool, dict):
        return ""
    name = tool.get("name")
    if not name and isinstance(tool.get("function"), dict):
        name = tool["function"].get("name")
    return name if isinstance(name, str) else ""


def _callable_tools(tools):
    """Only the tools the caller can actually execute for us.

    Captured from a live Codex CLI request: its `tools[]` also carries `{"type": "web_search",
    "name": null}` and `{"type": "namespace", ...}`. Those run on the vendor's side, not the
    caller's, and a nameless entry cannot be named in a name-based protocol — rendered into the
    prompt they are an offer nobody can answer.
    """
    return [t for t in tools or []
            if (t.get("type") if isinstance(t, dict) else None) in (None, "function")
            and _tool_name(t)]


def _tool_constraints(body):
    """The caller's `tool_choice` / `parallel_tool_calls`, as instruction text. "" when free."""
    parts = []
    if body.get("parallel_tool_calls") is False:
        # The templates invite several calls at once; Codex CLI asks for one.
        parts.append("Emit AT MOST ONE tool call in this reply.")
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        if choice.get("type") in ("any", "tool", "function"):
            named = choice.get("name") or (choice.get("function") or {}).get("name")
            parts.append(f"You MUST call {named or 'one of the tools above'} in this reply.")
    elif choice == "required":
        parts.append("You MUST call one of the tools above in this reply.")
    return " ".join(parts)


def _tools_disabled(choice):
    return choice == "none" or (isinstance(choice, dict) and choice.get("type") == "none")


def prepare(body, kind, protocol=None, wire="openai", tool_note=""):
    if wire == "responses":
        messages = _messages_from_responses_input(body)
        if not messages:
            raise SeatBadRequest("input is required.")
    else:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise SeatBadRequest("messages[] is required.")
    requested = str(body.get("model") or "")
    model_alias = alias_for(kind, requested)
    if model_alias is None:
        raise SeatBadRequest(
            f"This engine does not serve model {requested!r}. "
            f"Serves: {', '.join(advertised_models(kind))}."
        )
    resolved_protocol = protocol or (ANTHROPIC if wire == "anthropic" else OPENAI)
    tools = body.get("tools") if isinstance(body.get("tools"), list) else None
    if wire == "anthropic" and tools:
        _reject_server_tools(tools)
    tools = None if _tools_disabled(body.get("tool_choice")) else _callable_tools(tools)
    tool_text = resolved_protocol.render(tools) if tools else ""
    # Constraints, then the seat's note — the caller's own rules outrank ours, and both come after
    # the tool list so they are the last thing read about tools.
    if tool_text:
        for extra in (_tool_constraints(body), tool_note):
            if extra:
                tool_text = f"{tool_text}\n\n{extra}"
    # `keep_system`: a system prompt taken from a TOP-LEVEL field leaves any `role: "system"`
    # message in the transcript unread. Claude Code sends both — captured live, a 6379-character
    # system message sat inside `messages[]` and was dropped.
    prompt = json_prompt(messages, tool_text or None,
                         keep_system=wire in ("anthropic", "responses"))
    if wire == "anthropic":
        # `system` is a TOP-LEVEL field on this wire, not a `role: "system"` message — reading it
        # off `messages` (the OpenAI shape) would silently drop the caller's whole instruction set,
        # since Anthropic never puts one there. May be a plain string or a list of text blocks;
        # `_flatten_content` already reads both.
        system_prompt = _flatten_content(body.get("system"))
        system_prompt = system_prompt if system_prompt.strip() else DEFAULT_SYSTEM_PROMPT
    elif wire == "responses":
        # `instructions` is the TOP-LEVEL developer/system prompt on this wire — the analogue of
        # Anthropic's `system`, not a message. A `role: "system"` item in `input` is also honoured
        # (build_system_prompt extracts those), but `instructions` wins when both are present
        # because it is the canonical Responses spelling and the one a native caller sets.
        system_prompt = _flatten_content(body.get("instructions"))
        if not system_prompt.strip():
            system_prompt = build_system_prompt(messages)
    else:
        system_prompt = build_system_prompt(messages)
    return PreparedRequest(
        model=requested or f"{kind}:{model_alias}",
        model_alias=model_alias,
        prompt=prompt,
        system_prompt=system_prompt,
        tool_protocol=resolved_protocol if tools else None,
        wire=wire,
        effort=_resolve_effort(body),
        messages=tuple(m for m in messages if m.get("role") != "system"),
        tool_text=tool_text,
    )


# the subprocess call

def can_stream(spec):
    return spec.parse_event is not None or spec.run is not None


def run_seat(spec, prepared, binary, timeout):
    """One CLI run, in a scratch directory the adapter may write to and which is always removed.

    The prompt goes in on stdin and any system-prompt file lives in that directory — never argv,
    which `ps` exposes and which has a size limit a long transcript exceeds.

    `cwd` is that scratch directory too, so the CLI reports a throwaway path rather than whatever
    directory the seat server happens to have been started in.
    """
    if spec.run is not None:
        return spec.run(binary, prepared, timeout, None)
    with tempfile.TemporaryDirectory(prefix=f"grid-{spec.kind}-seat-") as tmp:
        tmpdir = Path(tmp)
        argv, stdin = spec.invoke(binary, prepared, tmpdir, stream=False)
        try:
            proc = subprocess.run(
                argv, input=stdin, capture_output=True, text=True, timeout=timeout,
                env=ensure_home(spec), cwd=tmpdir,
            )
        except subprocess.TimeoutExpired as exc:
            raise SeatError(f"`{spec.binary}` did not answer within {timeout:.0f}s.") from exc
        except OSError as exc:
            raise SeatError(f"Could not run `{binary}`: {exc}") from exc
        return spec.decode(proc, tmpdir)


def stream_seat(spec, prepared, binary, timeout):
    """Yield text as the CLI produces it, then one final SeatResult.

    Reads the CLI's own JSONL as it arrives. `decode` is reused on the collected output, so the
    final answer is built by the same code the non-streaming path uses and the two cannot drift.
    Only a spec with `parse_event` gets here; `can_stream` is the gate.
    """
    if spec.run is not None:
        return (yield from _stream_via_run(spec, prepared, binary, timeout))
    with tempfile.TemporaryDirectory(prefix=f"grid-{spec.kind}-seat-") as tmp:
        tmpdir = Path(tmp)
        argv, stdin = spec.invoke(binary, prepared, tmpdir, stream=True)
        # Turns to sit out before anything is forwarded. Counted from the stdin actually built, not
        # recomputed from `messages`, so it cannot drift from what the CLI was really handed.
        skip_turns = max(0, spec.expected_turns(stdin) - 1) if spec.expected_turns else 0
        turns_done = 0
        try:
            proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=ensure_home(spec), cwd=tmpdir,
            )
        except OSError as exc:
            raise SeatError(f"Could not run `{binary}`: {exc}") from exc

        # Feed stdin from a thread: a long transcript can exceed the pipe buffer, and writing it
        # inline would deadlock against a child already blocked writing its own output.
        writer = threading.Thread(target=_write_stdin, args=(proc, stdin), daemon=True)
        writer.start()

        lines, quota, deadline = [], [], time.monotonic() + timeout
        try:
            for line in proc.stdout:
                lines.append(line)
                if time.monotonic() > deadline:
                    proc.kill()
                    raise SeatError(f"`{spec.binary}` did not answer within {timeout:.0f}s.")
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # CLIs print non-JSON lines too; they are not errors
                if isinstance(event, dict):
                    parsed = spec.parse_event(event)
                    if isinstance(parsed, QuotaSnapshot):
                        quota.append(parsed)   # free quota reading, carried by the answer itself
                    elif isinstance(parsed, TurnEnd):
                        turns_done += 1
                    elif parsed and turns_done >= skip_turns:
                        # Everything before the last turn is the CLI re-answering a question the
                        # transcript had already settled. Forwarded, it arrived glued to the front
                        # of the real answer with no separator — the "…work on?Mochi" shape.
                        yield parsed
        finally:
            writer.join(timeout=1)
            proc.stdout.close()
            stderr = proc.stderr.read() if proc.stderr else ""
            proc.stderr.close()
            proc.wait(timeout=10)

        collected = SimpleNamespace(
            stdout="".join(lines), stderr=stderr, returncode=proc.returncode
        )
        result = spec.decode(collected, tmpdir)
        return (result, quota[-1] if quota else None)


def _stream_via_run(spec, prepared, binary, timeout):
    """Turn a `run` hook's delta CALLBACK into a generator, so both execution models present the
    same interface to the server. The hook runs on its own thread and pushes chunks through a
    queue; whatever it returns or raises comes back here.
    """
    chunks: queue.Queue = queue.Queue()
    done = object()
    outcome = []

    def worker():
        try:
            outcome.append(spec.run(binary, prepared, timeout, chunks.put))
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread below
            outcome.append(exc)
        finally:
            chunks.put(done)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while True:
        item = chunks.get()
        if item is done:
            break
        if item:
            yield item
    thread.join(timeout=5)
    result = outcome[0] if outcome else SeatError(f"`{spec.binary}` produced no result.")
    if isinstance(result, BaseException):
        raise result
    # Same (result, quota) return as the invoke/decode path — the caller unpacks one shape. A `run`
    # hook carries no free quota reading; `read_quota` is its own call.
    return (result, None)


def _write_stdin(proc, text):
    try:
        proc.stdin.write(text)
        proc.stdin.close()
    except (BrokenPipeError, ValueError):
        pass  # the child exited early; its own output explains why



def parse_calls(protocol, text):
    """`(prose, calls)` from a protocol, shouting when a call was attempted and did not parse.

    A malformed call still travels as text — it is the model's output and is not ours to repair or
    delete. But it must not travel SILENTLY: unannounced it reads like an ordinary answer, and the
    user is left staring at raw JSON with nothing saying a tool call failed. Measured on a live
    codex turn: the model wrote `{"tool_calls": …}` with one closing brace missing, the scan found
    nothing, and the whole object was printed as prose.
    """
    if protocol is None or not getattr(protocol, "parse", None):
        return text, []
    prose, calls = protocol.parse(text)
    if not calls and ('"tool_calls"' in text or '"tool_use"' in text):
        print(f"[cli-seat] a tool call did not parse ({len(text)}B); forwarded as text",
              file=sys.stderr, flush=True)
    return prose, calls


# answer -> OpenAI chat.completion

def to_chat_completion(result, kind, model, protocol=None):
    """A standard chat.completion, plus `grid_cli_seat` carrying the run's measured cost.

    A CLI has no native tool channel, so a tool call arrives as TEXT in whatever convention was put
    into the prompt. `protocol` (set only when the caller sent `tools[]`) reads it back into
    `tool_calls[]` — the shape every OpenAI client already understands, and the one the relay
    translates into an Anthropic `tool_use` block. Without it the text is returned verbatim, which
    is what a caller that brought its own convention wants.
    """
    text, tool_calls = parse_calls(protocol, result.text)
    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {
            "prompt_tokens": result.input_tokens,
            "completion_tokens": result.output_tokens,
            "total_tokens": result.input_tokens + result.output_tokens,
        },
        "grid_cli_seat": {
            "kind": kind,
            "total_cost_usd": result.cost_usd,
            "duration_ms": result.duration_ms,
            "num_turns": result.num_turns,
            "session_id": result.session_id,
        },
    }


def _to_tool_use(call):
    """A parsed call in EITHER protocol's shape -> an Anthropic `tool_use` block.

    The seat's protocol and the wire it answers on are chosen independently: codex parses with
    STRUCTURED (OpenAI-shaped calls) but may be asked on `/messages`. Without this the OpenAI shape
    was spliced into `content` verbatim and the client received a block of type "function".
    """
    if call.get("type") == "tool_use":
        return call
    function = call.get("function") or {}
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments, strict=False)
        except ValueError:
            arguments = {}
    return {"type": "tool_use",
            "id": call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
            "name": function.get("name") or "",
            "input": arguments if isinstance(arguments, dict) else {}}


def to_anthropic_message(result, kind, model, protocol=None):
    """The Anthropic `message` shape, built directly rather than via a chat.completion the relay
    would have to convert back."""
    text, calls = parse_calls(protocol, result.text)
    content = ([{"type": "text", "text": text}] if text else []) + [_to_tool_use(c) for c in calls]
    return {
        "id": f"msg_{uuid.uuid4().hex}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "tool_use" if calls else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens},
        "grid_cli_seat": {"kind": kind, "total_cost_usd": result.cost_usd,
                          "duration_ms": result.duration_ms, "num_turns": result.num_turns,
                          "session_id": result.session_id},
    }


def answer(spec, prepared, binary, timeout):
    result = run_seat(spec, prepared, binary, timeout)
    return to_chat_completion(result, spec.kind, prepared.model, prepared.tool_protocol)


def answer_anthropic(spec, prepared, binary, timeout):
    """`answer`'s Anthropic twin: the same one CLI run, decoded into the `message` shape
    `/messages` promises rather than a chat.completion."""
    result = run_seat(spec, prepared, binary, timeout)
    return to_anthropic_message(result, spec.kind, prepared.model, prepared.tool_protocol)


def _kinded(chunk):
    """One streamed chunk -> ("reasoning", text) or ("delta", text).

    Shared by the three `answer_stream*` wrappers so a seat's thinking cannot be labelled the answer
    on one wire and thinking on another.
    """
    return ("reasoning", chunk.text) if isinstance(chunk, Reasoning) else ("delta", chunk)


def answer_stream(spec, prepared, binary, timeout):
    """Yield ("delta", text) / ("reasoning", text) as the CLI produces it, then
    ("done", (completion, quota|None)).

    Tool calls ride the final ("done") event, never a delta: only a whole answer can be parsed,
    so nothing about a call is known until the CLI has finished writing it.
    """
    stream = stream_seat(spec, prepared, binary, timeout)
    while True:
        try:
            yield _kinded(next(stream))
        except StopIteration as stop:
            result, quota = stop.value
            completion = to_chat_completion(
                result, spec.kind, prepared.model, prepared.tool_protocol)
            yield "done", (completion, quota)
            return


def answer_stream_anthropic(spec, prepared, binary, timeout):
    """`answer_stream`'s Anthropic twin: identical deltas, the final event built by
    `to_anthropic_message` instead of `to_chat_completion`."""
    stream = stream_seat(spec, prepared, binary, timeout)
    while True:
        try:
            yield _kinded(next(stream))
        except StopIteration as stop:
            result, quota = stop.value
            message = to_anthropic_message(
                result, spec.kind, prepared.model, prepared.tool_protocol)
            yield "done", (message, quota)
            return


# answer -> OpenAI Responses API object

def to_response(result, kind, model, protocol=None):
    """A Responses API ``response`` object, built directly rather than via a chat.completion.

    Tool calls become ``function_call`` output items (the Responses dialect's native shape), not
    ``tool_calls`` on a message — the same parse that extracts them for chat feeds this instead, so
    a tool conversation round-trips identically on every wire the seat speaks.
    """
    text, tool_calls = parse_calls(protocol, result.text)
    # A call-only answer carries NO message item. It used to emit one with `content: []`, and a
    # Responses client reading `output[]` in order then saw an assistant message that said nothing
    # standing in front of the call. An empty message is only correct when there is nothing else to
    # report — an answer with neither text nor calls — so that one case still gets it.
    output = []
    if text or not tool_calls:
        output.append({
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}] if text else [],
        })
    for call in tool_calls:
        function = call.get("function") or {}
        output.append({
            "id": f"fc_{uuid.uuid4().hex[:24]}",
            "type": "function_call",
            "status": "completed",
            "call_id": call.get("id") or f"call_{uuid.uuid4().hex[:24]}",
            "name": function.get("name") or "",
            "arguments": function.get("arguments") or "{}",
        })
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output": output,
        "usage": {
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "total_tokens": result.input_tokens + result.output_tokens,
        },
        "grid_cli_seat": {
            "kind": kind,
            "total_cost_usd": result.cost_usd,
            "duration_ms": result.duration_ms,
            "num_turns": result.num_turns,
            "session_id": result.session_id,
        },
    }


def answer_response(spec, prepared, binary, timeout):
    """`answer`'s Responses twin: the same one CLI run, decoded into the ``response`` object
    ``/responses`` promises rather than a chat.completion."""
    result = run_seat(spec, prepared, binary, timeout)
    return to_response(result, spec.kind, prepared.model, prepared.tool_protocol)


def answer_stream_response(spec, prepared, binary, timeout):
    """`answer_stream`'s Responses twin: identical deltas, the final event built by
    `to_response` instead of `to_chat_completion`."""
    stream = stream_seat(spec, prepared, binary, timeout)
    while True:
        try:
            yield _kinded(next(stream))
        except StopIteration as stop:
            result, quota = stop.value
            response = to_response(
                result, spec.kind, prepared.model, prepared.tool_protocol)
            yield "done", (response, quota)
            return
