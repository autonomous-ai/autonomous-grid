"""Remote-mode `grid task create|get` — hand the grid a coding task and read the result back.

A task (ADR 0032) is work that outlives the request that created it: the client posts a prompt, a
provider claims it and runs an agent against it, and the client comes back later for the result.
That is why this is two commands and not one streaming call — there is nothing to hold open.

Each call resolves the grid + relay base + per-grid access token exactly as `price`/`router` do, and
talks to the relay's `/relay/v1/tasks`. Remote-only — `cli.dispatch` gates it (in `REMOTE_ONLY`);
local mode exits with guidance. Import rule mirrors the other remote handlers: stdlib only at module
top; `remote.*` / sibling cli modules imported lazily.
"""
from __future__ import annotations

import argparse
import json
import sys

# Bounded reattach: a relay that is genuinely gone must end the command rather than spin. The budget
# is per OUTAGE, not per task — any event received resets it, so a long task that blips repeatedly
# is not slowly starved of retries.
_RECONNECT_ATTEMPTS = 5
_RECONNECT_BACKOFF_SECONDS = 2.0


def _resolve(args: argparse.Namespace) -> tuple[str, str, str]:
    """(relay_base, access_token, label) for the selected grid. Clean SystemExit if signed-out."""
    from remote import credentials

    from . import remote_grid

    session = credentials.require_session()
    rec = remote_grid._select(getattr(args, "grid", None))
    network_id = remote_grid._network_id(rec)
    label = rec.get("name") or network_id
    token = rec.get("access_token")
    if not token:
        raise SystemExit(
            f"Grid {label} has no access token locally. Run `grid login` to refresh your grids.")
    base, _status = remote_grid.resolve_relay_base(session, rec, network_id, label)
    return base, token, label


def cmd_remote_task(args: argparse.Namespace) -> int:
    if args.subcommand == "create":
        return _task_create(args)
    if args.subcommand == "follow":
        return _task_follow(args)
    # argparse (required=True + choices) guarantees the rest is `get`; guard explicitly anyway, so
    # direct misuse of the handler fails loudly rather than falling through to the wrong verb.
    if args.subcommand != "get":
        raise SystemExit(f"Unknown task subcommand: {args.subcommand!r}")
    return _task_get(args)


def _task_create(args: argparse.Namespace) -> int:
    from remote import relay

    base, token, label = _resolve(args)
    task = relay.create_task(
        base, token, prompt=args.prompt, project=getattr(args, "project", None))

    if getattr(args, "json", False):
        print(json.dumps(task, indent=2))
        return 0

    # `.get()` throughout: the reply shape is the relay's, and an older one may not send every key.
    print(f"task {task.get('id') or '(no id)'} created on {label}")
    print(f"state={task.get('state') or 'unknown'}")
    print(f"project={task.get('project_id') or 'unknown'}")
    print(f"\nWatch it with: grid task get {task.get('id') or '<id>'}")
    return 0


def _task_follow(args: argparse.Namespace) -> int:
    """Watch a task's event log live, reattaching at the cursor if the connection drops.

    The cursor is the whole point. Every event carries its `seq`, so a stream that dies is resumed
    with `after_seq=<last seq seen>` and the relay replays exactly what follows — no gap, no repeat.
    Without that, a reconnect would either re-render everything or silently skip whatever arrived
    while the socket was down.

    Reconnects are BOUNDED. A relay that is genuinely gone must end the command, not spin forever;
    and an answered refusal (403 not yours, 404 no such task, 410 expired) is never retried at all,
    because reconnecting cannot change a verdict.

    Exit code is the task's own outcome: 0 for `completed`, 1 for `failed`/`timed_out`, so a script
    can branch on it. A stream that ends without a terminal event exits non-zero too — the task's
    fate is unknown, and reporting unknown as success is the failure this guards.
    """
    import time

    from remote import relay

    base, token, _label = _resolve(args)
    as_json = bool(getattr(args, "json", False))
    cursor = int(getattr(args, "after_seq", -1) or -1)
    terminal: dict | None = None
    attempts_left = _RECONNECT_ATTEMPTS

    while terminal is None:
        made_progress = False
        try:
            for seq, event in relay.stream_task_events(
                    base, token, args.task_id, after_seq=cursor):
                cursor = seq
                made_progress = True
                _render(seq, event, as_json=as_json)
                if event.get("type") == "task.terminal":
                    terminal = event
                    break
        except SystemExit:
            raise
        except relay.RelayError as exc:
            if _is_answer_not_blip(getattr(exc, "status", None)):
                print(f"Cannot follow task {args.task_id}: {exc}", file=sys.stderr)
                return 1
            if made_progress:
                attempts_left = _RECONNECT_ATTEMPTS
            elif attempts_left <= 0:
                print(f"Lost the connection following task {args.task_id} and could not "
                      f"reattach: {exc}", file=sys.stderr)
                return 1
            else:
                attempts_left -= 1
            print(f"connection lost ({exc}); reattaching at seq {cursor}", file=sys.stderr)
            time.sleep(_RECONNECT_BACKOFF_SECONDS)
            continue

        if terminal is not None:
            break

        # The stream ended CLEANLY without a terminal event, and the client cannot tell why: the
        # relay may have finished (its row GC'd), or something between us — a proxy's SSE read
        # timeout — may have closed a perfectly live stream. Those are byte-identical at this end,
        # so reattach rather than guess. Progress refills the budget, so a proxy that severs a busy
        # stream every few minutes is followed indefinitely; a task that is genuinely over yields
        # nothing new and costs a few empty reattaches before this gives up.
        if made_progress:
            attempts_left = _RECONNECT_ATTEMPTS
        elif attempts_left <= 0:
            print(f"The stream for task {args.task_id} ended without a terminal event.",
                  file=sys.stderr)
            return 1
        else:
            attempts_left -= 1
        time.sleep(_RECONNECT_BACKOFF_SECONDS)

    return 0 if terminal.get("state") == "completed" else 1


def _is_answer_not_blip(status: int | None) -> bool:
    """Whether the relay ANSWERED, so reattaching cannot change the outcome.

    The same rule the provider's report path uses, applied to the read side: a 4xx is a verdict
    (403 not yours, 404 no such task, 410 expired); a 5xx or a bare transport failure means nobody
    decided anything yet and the connection is worth remaking.
    """
    return status is not None and 400 <= status < 500


def _render(seq: int, event: dict, *, as_json: bool) -> None:
    """One event, printed. Unknown types are shown rather than dropped.

    Event types grow — issue 08 adds `task.tree` — and a follower that rendered only the types it
    knew would show a user nothing while the relay faithfully streamed them what they asked for.
    """
    if as_json:
        print(json.dumps({"seq": seq, "event": event}))
        return

    kind = event.get("type") or "event"
    if kind == "task.output":
        print(event.get("text", ""))
    elif kind == "task.tool_use":
        # The line a user reads most of — a task is minutes of tool calls and a sentence of prose.
        # The path is optional: `Bash` and `WebSearch` target nothing, and a stream that went silent
        # during a ten-minute test run would read as a hang.
        path = event.get("path")
        print(f"[{seq}] {event.get('tool') or 'tool'}" + (f" {path}" if path else ""))
    elif kind == "task.stderr":
        # The agent's own diagnostics, on OUR stderr — so `grid task follow > out.txt` keeps the
        # task's output separable from the noise around it.
        print(f"[{seq}] {event.get('text', '')}", file=sys.stderr)
    elif kind == "task.session":
        print(f"[{seq}] session {event.get('session_id')}")
    elif kind == "task.attempt_started":
        provider = event.get("provider_id")
        where = f" on {provider}" if provider else ""
        print(f"[{seq}] attempt {event.get('attempt')} started{where}")
    elif kind == "task.terminal":
        error = event.get("error")
        print(f"[{seq}] {event.get('state')}" + (f": {error}" if error else ""))
    else:
        # Verbatim, minus the type we already printed — enough to be useful without pretending to
        # understand a shape this build has never seen.
        rest = {k: v for k, v in event.items() if k != "type"}
        print(f"[{seq}] {kind} {json.dumps(rest, sort_keys=True)}" if rest else f"[{seq}] {kind}")


def _task_get(args: argparse.Namespace) -> int:
    from remote import relay

    base, token, _label = _resolve(args)
    task = relay.get_task(base, token, args.task_id)

    if getattr(args, "json", False):
        print(json.dumps(task, indent=2))
        return 0

    print(f"task {task.get('id') or args.task_id}")
    print(f"state={task.get('state') or 'unknown'}")
    if task.get("provider_id"):
        print(f"provider={task['provider_id']}")
    if task.get("claude_session_id"):
        print(f"session={task['claude_session_id']}")
    if task.get("error"):
        print(f"error={task['error']}")
    result = task.get("result_text")
    if result:
        print("\n--- result ---")
        print(result.rstrip("\n"))
    return 0
