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
    if task.get("error"):
        print(f"error={task['error']}")
    result = task.get("result_text")
    if result:
        print("\n--- result ---")
        print(result.rstrip("\n"))
    return 0
