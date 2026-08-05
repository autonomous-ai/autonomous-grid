"""Claude Code's `stream-json` output, turned into a task's event log (ADR 0032, issue 03).

One `StreamTranslator` per task child. It is fed one line at a time as the child writes it and
returns the events to publish, so a client watching the task sees tool calls while they happen
rather than a transcript after the fact (ADR 0032 D-f).

Two rules shape everything here.

**It translates, it does not forward.** The relay stores an event whole and refuses any over 64 KiB
(`task_events.MAX_EVENT_BYTES`), and a single `stream-json` record routinely exceeds that — one
`tool_result` carrying a read file is enough. Forwarding raw would mean the relay refusing exactly
the events a user most wants to see, and would make every client a parser of Claude Code's schema.
So each record becomes a small, typed event: a tool call is its name and its target path, not its
arguments.

**It never raises.** It is called from the loop that is running the child, and the caller's job is
to finish the task and report it. A malformed line is a line — not an incident, and never a reason
to lose the run. Everything unrecognised is ignored rather than guessed at, which is also what makes
`rate_limit_event` (issue 09) and any record a future version adds free to arrive.
"""
from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from typing import Any

from .task_events import MAX_EVENT_BYTES

Event = tuple[str, dict[str, Any]]

# Characters of one string that fit inside `task_events.MAX_EVENT_BYTES` in the worst case, with room
# left for the event's other keys. The budget is in CHARACTERS against a bytes limit, so it is
# divided by the worst case: a character can reach 6 bytes once JSON escaping (`\uXXXX`) is counted,
# not merely 4 for UTF-8.
MAX_TEXT_CHARS = MAX_EVENT_BYTES // 6 - 200
TRUNCATION_MARKER = "… [truncated]"


# Credential shapes that must never survive into a published event. The provider's own credential is
# what runs the agent, and the agent is executing a prompt a stranger wrote: `cat
# ~/.claude/.credentials.json` is a legal thing for a task to ask, and its output comes straight back
# down this stream into a log the requesting user reads. Anthropic keys carry a fixed prefix, so they
# are matchable; `Bearer` covers the shape a credential takes passing through a terminal.
_REDACTIONS = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{4,}"), "sk-ant-***"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer ***"),
)


def redact(text: str) -> str:
    """`text` with anything credential-shaped replaced by a marker."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _redacted(events: list[Event]) -> list[Event]:
    """Every string in every event, scrubbed on the way OUT of the translator.

    Applied here rather than at each construction site on purpose: a per-site rule is one a future
    event type forgets, and forgetting is silent — the event publishes, the client renders it, and
    the credential is in a durable log on the relay with no way to unsay it.
    """
    return [
        (name, {k: redact(v) if isinstance(v, str) else v for k, v in fields.items()})
        for name, fields in events
    ]


def bounded(text: str) -> str:
    """`text`, trimmed to fit inside one event.

    The relay refuses any event over `MAX_EVENT_BYTES` and a refused batch takes the events around
    it with it, so an over-long string is bounded on this side rather than resent forever. Truncation
    is MARKED, because output that silently stops mid-line reads like a crash.
    """
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return text[:MAX_TEXT_CHARS] + TRUNCATION_MARKER


class StreamTranslator:
    """One task child's stream. Feed it lines; publish what it returns."""

    def __init__(self, on_rate_limit: Callable[[Any], None] | None = None) -> None:
        #: Where a `rate_limit_event`'s body goes: the provider's own capacity gate (issue 09).
        #: Optional, because the child is the point and this is an observer of it — a caller with no
        #: gate wired runs a task exactly as it did before the gate existed.
        self._on_rate_limit = on_rate_limit
        #: Whether the gate has already been reported as broken. `rate_limit_event` arrives with
        #: every turn of the agent's output, so a gate that raises would otherwise print one warning
        #: per turn for the rest of the run — the same flood `_Reporter._complain` exists to stop.
        self._gate_broken = False
        #: The conversation this run opened, so the project's next task can `--resume` it (issue 06).
        self.session_id: str | None = None
        #: The agent's final message — what the task stores as its `output`.
        self.result_text: str | None = None
        #: The agent's own verdict on the run, and its reason. `is_error` is believed; a *success*
        #: claim is not, because the exit status is the authority for that (issue 03).
        self.is_error: bool = False
        self.subtype: str | None = None

    def feed(self, line: str) -> list[Event]:
        """One line of the child's stdout as zero or more events to publish. Never raises.

        A line that is not a record becomes text rather than an error. That covers three different
        things, and they are deliberately handled the same way:

          * **A plain-text notice.** The CLI prints those alongside its events, and the one line
            explaining why a task did nothing is exactly the one worth keeping.
          * **A line that will not parse at all**, including one that raises `RecursionError` —
            which `json.loads` does on deep nesting and which is NOT a `ValueError`, so a guard
            naming only `ValueError` has a hole no malformed-input sweep finds.
          * **Valid JSON that is not an object** (`123`, `"x"`, `[1]`). These parse fine and then
            have no `.get`, so treating them as records raises `AttributeError` deeper in — the same
            lost task as a parse failure, arriving somewhere harder to read.
        """
        stripped = line.strip()
        if not stripped:
            return []
        try:
            record = json.loads(stripped)
        except (ValueError, RecursionError):
            record = None
        if not isinstance(record, dict):
            return _redacted([("task.output", {"text": bounded(stripped)})])
        return _redacted(self._translate(record))

    def _translate(self, record: dict[str, Any]) -> list[Event]:
        kind = record.get("type")
        if kind == "system" and record.get("subtype") == "init":
            return self._session(record)
        if kind == "assistant":
            return self._assistant(record)
        if kind == "user":
            return self._tool_results(record)
        if kind == "result":
            return self._result(record)
        if kind == "rate_limit_event":
            return self._rate_limit(record)
        # Everything else — `stream_event`, whatever a future version adds — is ignored rather than
        # guessed at. That is what makes a new record type free to arrive: an old provider narrates a
        # little less, and nothing breaks.
        return []

    def _rate_limit(self, record: dict[str, Any]) -> list[Event]:
        """The provider's own subscription, reporting itself (ADR 0032 issue 09).

        It goes two places, and they want different things. The GATE wants the vendor's body
        verbatim — deciding what a payload means is `task_capacity`'s job, and a second reading here
        is a second place to disagree with it. The CLIENT wants three flat fields: a user whose task
        sat queued is owed the reason, and the rest of the vendor's object says nothing they can act
        on.

        Nothing here is believed enough to fail on. The record arrived off a subprocess's stdout, so
        every field is optional and every type is a guess; a shape this build has never seen becomes
        an event with nulls in it, never an exception in the loop running the child.
        """
        info = record.get("rate_limit_info")
        self._tell_the_gate(info)
        fields = info if isinstance(info, dict) else {}
        status = fields.get("status")
        limit_type = fields.get("rateLimitType")
        resets_at = fields.get("resetsAt")
        return [("task.rate_limit", {
            "status": bounded(status) if isinstance(status, str) else None,
            "limit_type": bounded(limit_type) if isinstance(limit_type, str) else None,
            # A bool is an `int` in Python, and `"resets_at": true` in a rendered event reads as a
            # timestamp nobody can parse rather than as the junk it is.
            "resets_at": resets_at
            if isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool) else None,
        })]

    def _tell_the_gate(self, info: Any) -> None:
        """Hand the reading to the capacity gate. A fault there may never reach the child's loop.

        The gate is documented never to raise; this does not rest on that. `feed`'s whole contract is
        that a line is a line and never an incident, and an escape here would unwind past the
        supervisor and fail a task that is running perfectly — over an observer.
        """
        if self._on_rate_limit is None:
            return
        try:
            self._on_rate_limit(info)
        except (Exception, SystemExit) as exc:
            # `SystemExit` is this repo's clean-error idiom and is NOT an `Exception`; a guard naming
            # only `Exception` would let it kill the thread running the child.
            #
            # Said ONCE per run, the lesson `_Reporter._complain` already records: this record
            # arrives with every turn of the agent's output, so a warning per record would bury the
            # one line that matters under its own repetition.
            if self._gate_broken:
                return
            self._gate_broken = True
            print(f"\n[tasks] the task capacity gate raised on a rate-limit reading ({exc!r}) — it "
                  f"is documented never to. This provider keeps claiming tasks and the running task "
                  f"is unaffected; readings are being dropped from here on.", file=sys.stderr)

    def _tool_results(self, record: dict[str, Any]) -> list[Event]:
        """A `user` record is a tool RESULT. Only the fact and the outcome travel.

        Its content is whatever the tool produced — a whole file for a `Read` — which belongs in
        neither an event nor the durable log the relay keeps of one.
        """
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        events: list[Event] = []
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                events.append(("task.tool_result", {
                    "id": block.get("tool_use_id"),
                    "is_error": bool(block.get("is_error")),
                }))
        return events

    def _result(self, record: dict[str, Any]) -> list[Event]:
        """The terminal record: the agent's answer, and its own account of how the run ended.

        No `task.terminal` is emitted. That event is the RELAY's, written inside the same transaction
        as the terminal state change (issue 02) precisely so it cannot be lost — a second copy from
        here would put two different endings in one log.
        """
        self._capture_session(record)
        text = record.get("result")
        if isinstance(text, str):
            # Redacted at capture: this leaves the module as the task's stored `output`, by a
            # different door than the events, so `_redacted` never sees it.
            self.result_text = redact(text)
        self.is_error = bool(record.get("is_error"))
        subtype = record.get("subtype")
        self.subtype = subtype if isinstance(subtype, str) else None
        return [("task.result", {
            "subtype": self.subtype,
            "is_error": self.is_error,
            "num_turns": record.get("num_turns"),
            "duration_ms": record.get("duration_ms"),
        })]

    def _assistant(self, record: dict[str, Any]) -> list[Event]:
        """One turn's content blocks, in the order the agent produced them."""
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        events: list[Event] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    events.append(("task.output", {"text": bounded(text)}))
            elif block.get("type") == "tool_use":
                events.append(_tool_use(block))
        return events

    def _session(self, record: dict[str, Any]) -> list[Event]:
        if not self._capture_session(record):
            return []
        return [("task.session", {"session_id": self.session_id})]

    def _capture_session(self, record: dict[str, Any]) -> bool:
        """Remember the run's session id, from whichever record carried it first.

        Both `system/init` and the terminal `result` carry it. Taking it from either means a run
        whose opening line was lost — to a torn write, or to a child killed before it flushed — still
        has one to store, and issue 06 can still resume the project's conversation.
        """
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return False
        self.session_id = session_id
        return True


# The keys a tool uses to name the thing it is acting on, in the order they are preferred. Only
# these — a tool's `input` is its whole argument object, and an `Edit` carries the entire old and new
# text, so anything broader would put a 90 KB blob inside an event the relay caps at 64 KiB.
_PATH_KEYS = ("file_path", "path", "notebook_path")


def _tool_use(block: dict[str, Any]) -> Event:
    """One `tool_use` block as the two things a follower needs: what ran, and on what."""
    fields = block.get("input")
    path = None
    for key in _PATH_KEYS:
        value = fields.get(key) if isinstance(fields, dict) else None
        if isinstance(value, str) and value:
            path = bounded(value)
            break
    name = block.get("name")
    return ("task.tool_use", {
        "tool": bounded(name) if isinstance(name, str) and name else "(unnamed tool)",
        "path": path,
        "id": block.get("id"),
    })
