"""Claude Code adapter. Everything specific to the `claude` binary lives here."""
from __future__ import annotations

import json
import re

from shared.agent.cli_seat import QuotaSnapshot, SeatError, SeatResult, SeatSpec

BINARY = "claude"
LABEL = "Claude Code"

# Exits 0 when signed in.
SIGNIN_ARGV = ("auth", "status")


def _as_text(value):
    """Anything unsendable, verbatim as text. Never dropped, never summarised."""
    return {"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}


def _as_anthropic_image(block):
    """OpenAI `image_url` / `input_image` -> Anthropic `image`. None if there is no url to carry."""
    source = block.get("image_url") if isinstance(block.get("image_url"), dict) else {}
    url = source.get("url") or block.get("url") or block.get("image_url")
    if not isinstance(url, str) or not url:
        return None
    if not url.startswith("data:"):
        return {"type": "image", "source": {"type": "url", "url": url}}
    head, _, data = url.partition(",")
    if not data:
        return None
    return {"type": "image", "source": {"type": "base64",
                                        "media_type": head[5:].split(";")[0] or "image/png",
                                        "data": data}}


# Caller-side request metadata, not content. `cache_control` belongs to the caller's own API call —
# forwarded into ours it 400s (`ttl='1h'` needs a beta header this seat does not send).
STRIPPED_KEYS = ("cache_control",)


def _passthrough_blocks(content):
    """Blocks forwarded as-is, in their original order.

    `claude` is Anthropic-native, so a block this file does not know is still one the CLI may.
    Nothing is dropped or replaced with a marker: what cannot go as-is goes as text.
    """
    blocks = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            blocks.append(_as_text(block))
            continue
        if any(key in block for key in STRIPPED_KEYS):
            block = {k: v for k, v in block.items() if k not in STRIPPED_KEYS}
        kind = block.get("type")
        if kind == "text":
            # An empty text block 400s, so it travels as its own JSON rather than being deleted.
            blocks.append(block if block.get("text") else _as_text(block))
        elif kind == "tool_result":
            blocks.append(block if block.get("content")
                          else {**block, "content": json.dumps(block, ensure_ascii=False)})
        elif kind in ("image_url", "input_image"):
            blocks.append(_as_anthropic_image(block) or _as_text(block))
        else:
            blocks.append(block)
    return blocks


def to_stream_json(messages, tools_text=None):
    """Convert OpenAI/Anthropic messages to Claude --input-format stream-json lines.

    System messages are skipped (system prompt goes via --system-prompt-file).
    OpenAI tool_calls become Anthropic tool_use blocks; role:tool becomes user + tool_result.
    Consecutive tool results are merged into ONE user message (Claude requires alternating
    user/assistant turns). tools_text (if set) is appended to the last user message.

    Empty content 400s (`text content blocks must be non-empty`), so a message with nothing in it
    travels as its own JSON rather than being dropped or padded with invented text.
    """
    return "\n".join(
        json.dumps({"type": m["role"], "message": m}, ensure_ascii=False)
        for m in conversation(messages, tools_text)
    )


def conversation(messages, tools_text=None):
    """The transcript as Anthropic messages: `[{"role", "content"}, …]`.

    OpenAI `tool_calls` become `tool_use`; `role: "tool"` becomes a user `tool_result`, consecutive
    ones merged into one message. Blocks keep their type and order. `tools_text`, if set, is
    appended to the last user message.
    """
    out, last_user = [], None
    pending = []

    def flush():
        nonlocal last_user
        if pending:
            out.append({"role": "user", "content": pending[:]})
            last_user = len(out) - 1
            pending.clear()

    for msg in messages:
        role, content = msg.get("role"), msg.get("content")
        if role == "system":
            continue
        if role == "tool":
            pending.append(_passthrough_blocks([{
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content,
            }])[0])
            continue
        flush()
        if role == "assistant":
            blocks = _passthrough_blocks(content)
            for call in msg.get("tool_calls") or []:
                fn = (call or {}).get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                blocks.append({"type": "tool_use", "id": call.get("id", ""),
                               "name": fn.get("name", ""),
                               "input": args if isinstance(args, dict) else {}})
            out.append({"role": "assistant", "content": blocks or [_as_text(msg)]})
            continue
        content = _passthrough_blocks(content) if isinstance(content, list) else content
        out.append({"role": role or "user", "content": content or [_as_text(msg)]})
        last_user = len(out) - 1

    flush()

    if tools_text:
        if last_user is None:
            out.append({"role": "user", "content": [{"type": "text", "text": tools_text}]})
        else:
            c = out[last_user]["content"]
            c = [{"type": "text", "text": c}] if isinstance(c, str) else list(c)
            out[last_user]["content"] = c + [{"type": "text", "text": tools_text}]
    return out




# Three flags carry the whole isolation story, each verified on the real binary:
#   --safe-mode         NOT --bare. --bare takes only ANTHROPIC_API_KEY and never reads OAuth or
#                       the keychain, so a subscription seat under it answers "Not logged in".
#   --tools ""          removes every built-in tool, so the model must WRITE a tool call as text.
#   --system-prompt-file REPLACES Claude Code's own prompt with the caller's.
#
# Replacing, not appending, is a privacy decision. Claude Code's default prompt carries the
# machine's cwd, OS and git status, so a seat that kept it answered a bare `pwd?` with the
# provider's real path and username — no tool needed. Replacing also drops ~5000 prompt tokens
# per request. The cost: this serves plain Claude, not Claude Code, so a benchmark measuring the
# two combined must use a different seat.
def invoke(binary, prepared, tmpdir, stream=False):
    """One turn, one prompt on stdin.

    `--input-format stream-json` cannot replay a transcript: every `user` line is a live turn, and
    claude's own answer to each is inserted into the history before the next runs. Measured — the
    replay of [user, assistant(tool_use), user(tool_result)] produced:

        result #1: ''                                              <- claude's own turn, empty
        result #2: 'API Error: 400 messages: text content blocks must be non-empty'

    That empty turn is claude's, not ours, so no amount of filtering on the way in removes it. The
    same interleaving splits a `tool_use` from its `tool_result`, which is the `messages.N.content.M`
    400 and the repeated identical tool call.

    `to_stream_json` is kept and tested for the session-based path (`--session-id`/`--resume`),
    which is the only way to replay structure without spawning turns.
    """
    system_file = tmpdir / "system.txt"
    system_file.write_text(prepared.system_prompt, encoding="utf-8")
    argv = [
        binary, "-p",
        "--safe-mode",
        "--tools", "",
        "--model", prepared.model_alias,
        "--no-session-persistence",
        "--system-prompt-file", str(system_file),
    ]
    if prepared.effort:
        argv += ["--effort", prepared.effort]
    argv += (["--output-format", "stream-json", "--include-partial-messages", "--verbose"]
             if stream else ["--output-format", "json"])
    return argv, prepared.prompt


def _json_lines(raw):
    """Every line of `raw` that parses as JSON. Non-JSON lines are skipped, not errors — the CLI
    prints plain-text notices alongside its events."""
    for line in raw.splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _last_result(events):
    """The final `result` element of an event sequence, or None. Last, not first: a retried turn
    emits more than one, and the last is the answer actually delivered."""
    found = None
    for event in events:
        if isinstance(event, dict) and event.get("type") == "result":
            found = event
    return found


def decode(proc, tmpdir):
    """The final `result` envelope, from any of the three output shapes.

    `--output-format json` prints EITHER the bare result object (older builds) or a JSON array of
    the whole event sequence (2.1.220); `stream-json` prints those events one per line. Handling
    all three here is what lets the streaming path reuse this function, so the modes cannot
    produce different answers.

    The array shape is why the parse and the fallback are no longer try/except partners: an array
    parses *successfully*, so keying the event scan off a JSONDecodeError meant a valid array
    yielded no envelope and every non-streaming request failed with the message below — while the
    answer sat in the array unread.
    """
    raw = (proc.stdout or "").strip()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        envelope = parsed
    elif isinstance(parsed, list):
        envelope = _last_result(parsed)
    else:
        envelope = _last_result(_json_lines(raw))   # stream-json: one event per line
    if envelope is None:
        detail = raw or (proc.stderr or "").strip() or f"exit code {proc.returncode}"
        raise SeatError(f"`claude` returned no result envelope: {detail[:400]}")

    text = str(envelope.get("result") or "")
    if envelope.get("is_error"):
        raise SeatError(text or "`claude` reported an error with no message.")

    usage = envelope.get("usage") or {}
    return SeatResult(
        text=text,
        # Cache reads and writes are real input the seat paid for; folding them in keeps
        # prompt_tokens comparable to an engine with no cache.
        input_tokens=int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cost_usd=float(envelope.get("total_cost_usd") or 0.0),
        duration_ms=int(envelope.get("duration_ms") or 0),
        num_turns=int(envelope.get("num_turns") or 0),
        session_id=str(envelope.get("session_id") or ""),
    )


# `/usage` is a local slash command, not a model turn — measured 0 tokens, 0 cost.
QUOTA_ARGV = ("-p", "/usage", "--output-format", "json",
              "--safe-mode", "--tools", "", "--no-session-persistence")

# `/usage` prints prose, not JSON. Only the two percentage lines are read; the breakdown below
# them is local-machine-only. Fail-open by contract — an unparseable screen means "keep serving".
_SESSION = re.compile(r"Current session:\s*(\d+)%\s*used(?:[^\n]*?resets\s+([^\n(]+))?", re.I)
_WEEK = re.compile(r"Current week \(all models\):\s*(\d+)%\s*used(?:[^\n]*?resets\s+([^\n(]+))?", re.I)


def _reset_text(info):
    """`resetsAt` is a unix timestamp — render it the way `/usage` prints resets, so both quota
    sources produce the same kind of string for a human."""
    import datetime

    stamp = info.get("resetsAt")
    if not stamp:
        return ""
    try:
        return datetime.datetime.fromtimestamp(int(stamp)).strftime("%b %-d at %-I:%M%p").lower()
    except (TypeError, ValueError, OSError):
        return ""


def parse_usage(text):
    session = _SESSION.search(text or "")
    if session is None:
        return None
    week = _WEEK.search(text or "")
    return QuotaSnapshot(
        session_pct=int(session.group(1)),
        week_pct=int(week.group(1)) if week else 0,
        session_reset=(session.group(2) or "").strip(),
        week_reset=(week.group(2) or "").strip() if week else "",
    )


def expected_turns(stdin):
    """How many turns this stdin will make `claude` run — one per `user` line.

    Counted off the built stdin rather than off `messages`, so it cannot disagree with what
    `to_stream_json` actually emitted. At least one: a request always runs a turn.
    """
    turns = 0
    for line in (stdin or "").splitlines():
        try:
            turns += json.loads(line).get("type") == "user"
        except ValueError:
            continue
    return max(1, turns)


def parse_event(event):
    """One streamed event -> the text to forward, a QuotaSnapshot, or None.

    `rate_limit_event` rides along with every answer for free, so the seat learns its own state
    without paying for a separate `/usage` probe. It reports whether the account is still allowed
    and when the window resets, but NOT a percentage — so it complements `parse_usage`, which
    gives the percentage but costs a process.
    """
    if event.get("type") == "rate_limit_event":
        info = event.get("rate_limit_info") or {}
        if info.get("status") != "allowed":
            # Blocked: report a full window so any ceiling refuses, and the reset the vendor gave.
            return QuotaSnapshot(session_pct=100, week_pct=100,
                                 session_reset=_reset_text(info), week_reset=_reset_text(info))
        return None
    # End of ONE turn, not of the request: a replayed transcript runs one per `user` line, and
    # `stream_seat` counts these to know when the live turn has finally started.
    if event.get("type") == "result":
        from shared.agent.cli_seat import TurnEnd

        return TurnEnd()
    if event.get("type") != "stream_event":
        return None
    inner = event.get("event") or {}
    if inner.get("type") != "content_block_delta":
        return None
    delta = inner.get("delta") or {}
    if delta.get("type") == "text_delta":
        return delta.get("text")
    # Extended thinking, when the caller asked for an effort. Returned as `Reasoning` rather than a
    # bare string so it cannot be mistaken for the answer — the tool parse must never read it, and a
    # `--effort` turn used to sit silent until the thinking finished.
    if delta.get("type") == "thinking_delta":
        from shared.agent.cli_seat import Reasoning

        text = delta.get("thinking") or ""
        return Reasoning(text) if text else None
    return None


SPEC = SeatSpec(
    kind="claude",
    binary=BINARY,
    # TEMPORARY: shares the operator's ~/.claude. The isolated home works (CLAUDE_CONFIG_DIR is
    # honoured, verified) but needs its own interactive sign-in, and that is the app's job to
    # drive. Set `home_env="CLAUDE_CONFIG_DIR"` to switch back once the app can log a seat in.
    #
    # What this costs meanwhile: the operator's chat history and settings sit in the same
    # directory the seat reads. `--safe-mode` still drops CLAUDE.md, skills, plugins, hooks and
    # MCP, and `--tools ""` means the model cannot reach any of it — but the isolation is one
    # layer thinner than codex's until the sign-in lands.
    home_env="",
    label=LABEL,
    signin_argv=SIGNIN_ARGV,
    login_argv=("auth", "login"),
    invoke=invoke,
    decode=decode,
    quota_argv=QUOTA_ARGV,
    parse_usage=parse_usage,
    parse_event=parse_event,
    expected_turns=expected_turns,
)
