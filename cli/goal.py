"""Remote ``grid goal`` surface over the relay's durable Goal conversations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _resolve(args: argparse.Namespace) -> tuple[str, str, str]:
    from .remote_task import _resolve
    return _resolve(args)


def _tools(path: str | None) -> list[dict]:
    if not path:
        return []
    source = Path(path).expanduser()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Could not read Goal tools from {source}: {exc}") from None
    if isinstance(value, dict) and value.get("version") == 1:
        value = value.get("tools")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SystemExit("Goal tools must be a JSON array, or an object with version 1 and tools array.")
    return value


def _show(goal: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(goal, indent=2))
        return
    print(f"{goal.get('id')} [{goal.get('status')}]")
    print(f"  objective  {goal.get('objective')}")
    print(f"  done when  {goal.get('done_when')}")
    print(f"  progress   {goal.get('turns_completed', 0)} turns · "
          f"{goal.get('tokens_used', 0)} tokens")
    if goal.get("turn_id"):
        print(f"  first turn {goal['turn_id']}")


def cmd_goal(args: argparse.Namespace) -> int:
    from remote import relay

    from . import project_arg
    from .remote_task import _resolve_project

    args = project_arg.resolve(args)
    base, token, _label = _resolve(args)
    if args.goal_action == "run":
        project_id = _resolve_project(base, token, args.project)
        goal = relay.create_goal(
            base, token, project_id=project_id, objective=args.objective,
            done_when=args.done_when, model=args.model, token_budget=args.token_budget,
            tools=_tools(args.tools), name=args.name)
        _show(goal, args.json)
        return 0
    if args.goal_action == "list":
        rows = relay.list_goals(base, token, all=args.all)
        if args.json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("No active Goals." if not args.all else "No Goal history.")
        else:
            for goal in rows:
                print(f"  {goal.get('status')!s:<15} {goal.get('id')}  {goal.get('objective')}")
        return 0
    if args.goal_action == "status":
        goal = relay.get_goal(base, token, args.goal_id)
    else:
        goal = relay.control_goal(base, token, args.goal_id, args.goal_action)
    _show(goal, args.json)
    return 0
