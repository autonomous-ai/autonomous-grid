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
import re
import shutil
import subprocess
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
    parse_event: object = None      # (event) -> str to stream | QuotaSnapshot | None
    home_env: str = ""              # env var pointing the CLI at its own home; "" = shares the user's
    config_files: tuple = ()        # ((relative path, text), …) written into that home


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
    whitelist = api_catalog.WHITELISTS.get(kind)
    if whitelist is None:
        return []
    return [api_catalog.advertised_name(kind, entry) for entry in whitelist.entries]


def alias_for(kind, advertised):
    """`claude:sonnet` -> `sonnet`. Bare names resolve too — the relay rewrites to the upstream
    spelling before forwarding, so the seat receives both forms."""
    name = (advertised or "").strip()
    entry = api_catalog.find_advertised(kind, name)
    if entry is not None:
        return entry.vendor_name
    whitelist = api_catalog.WHITELISTS.get(kind)
    if whitelist and any(e.vendor_name == name for e in whitelist.entries):
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
    """Only text survives; a CLI seat has no image path, so images are named as omitted."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                chunks.append(str(part.get("text") or ""))
            elif part.get("type") in ("image_url", "input_image"):
                chunks.append("[image omitted — this engine serves text only]")
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
    decoder = json.JSONDecoder()
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
            arguments = json.loads(arguments)
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


OPENAI = ToolProtocol(name="openai", render=render_openai_tools, parse=parse_openai_tool_calls)


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


def build_prompt(messages):
    """The non-system messages as one stdin prompt. Each request is a fresh process with no
    memory, so the whole transcript is replayed. A lone user turn passes through verbatim."""
    turns = [m for m in messages if m.get("role") != "system"]
    if not turns:
        raise SeatBadRequest("messages[] must contain at least one non-system message.")
    if len(turns) == 1 and turns[0].get("role") == "user":
        return _flatten_content(turns[0].get("content"))

    lines = []
    for message in turns:
        role = message.get("role")
        text = _flatten_content(message.get("content"))
        if role == "user":
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
            lines.append("\n".join(rendered))
        elif role == "tool":
            lines.append(f"Tool result: {text}")
        else:
            lines.append(text)
    return "\n\n".join(line for line in lines if line)


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


def prepare(body, kind, protocol=None):
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
    tools = body.get("tools") if isinstance(body.get("tools"), list) else None
    prompt = build_prompt(messages)
    if tools:
        prompt = f"{prompt}\n\n{(protocol or OPENAI).render(tools)}"
    return PreparedRequest(
        model=requested or f"{kind}:{model_alias}",
        model_alias=model_alias,
        prompt=prompt,
        system_prompt=build_system_prompt(messages),
        tool_protocol=(protocol or OPENAI) if tools else None,
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
                    elif parsed:
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


# answer -> OpenAI chat.completion

def to_chat_completion(result, kind, model, protocol=None):
    """A standard chat.completion, plus `grid_cli_seat` carrying the run's measured cost.

    A CLI has no native tool channel, so a tool call arrives as TEXT in whatever convention was put
    into the prompt. `protocol` (set only when the caller sent `tools[]`) reads it back into
    `tool_calls[]` — the shape every OpenAI client already understands, and the one the relay
    translates into an Anthropic `tool_use` block. Without it the text is returned verbatim, which
    is what a caller that brought its own convention wants.
    """
    text, tool_calls = result.text, []
    if protocol is not None and getattr(protocol, "parse", None):
        text, tool_calls = protocol.parse(result.text)
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


def answer(spec, prepared, binary, timeout):
    result = run_seat(spec, prepared, binary, timeout)
    return to_chat_completion(result, spec.kind, prepared.model, prepared.tool_protocol)


def answer_stream(spec, prepared, binary, timeout):
    """Yield ("delta", text) as the CLI produces it, then ("done", (completion, quota|None)).

    Tool calls ride the final ("done") event, never a delta: only a whole answer can be parsed,
    so nothing about a call is known until the CLI has finished writing it.
    """
    stream = stream_seat(spec, prepared, binary, timeout)
    while True:
        try:
            yield "delta", next(stream)
        except StopIteration as stop:
            result, quota = stop.value
            completion = to_chat_completion(
                result, spec.kind, prepared.model, prepared.tool_protocol)
            yield "done", (completion, quota)
            return
