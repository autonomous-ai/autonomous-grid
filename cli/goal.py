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


def _evals(path: str | None) -> list[dict]:
    if not path:
        return []
    source = Path(path).expanduser()
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Could not read Goal evals from {source}: {exc}") from None
    if isinstance(value, dict) and value.get("version") == 1:
        value = value.get("evals")
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise SystemExit(
            "Goal evals must be a JSON array, or an object with version 1 and evals array.")
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
    if goal.get("token_budget") is not None:
        reserved = int(goal.get("child_tokens_reserved") or 0)
        suffix = f" · {reserved:,} reserved for children" if reserved else ""
        print(f"  budget     {int(goal.get('tokens_used') or 0):,} / "
              f"{int(goal['token_budget']):,} tokens{suffix}")
    if goal.get("agents"):
        print(f"  agents     {', '.join(goal['agents'])}")
    if goal.get("parent_goal_id"):
        print(f"  parent     {goal['parent_goal_id']}")
    if goal.get("blocked_reason"):
        print(f"  blocked    {goal['blocked_reason']}")
    if goal.get("evals"):
        last = goal.get("last_eval") or {}
        results = last.get("results") or []
        passed = sum(bool(item.get("passed")) for item in results)
        suffix = f" · last {passed}/{len(results)} passed" if results else ""
        print(f"  evals      {len(goal['evals'])} checks{suffix}")
    if goal.get("turn_id"):
        print(f"  first turn {goal['turn_id']}")
    children = goal.get("children") or []
    if children:
        print(f"  children   {len(children)}")
        for child in children:
            required = "required" if child.get("required", True) else "optional"
            print(f"    {child.get('id')} [{child.get('status')}] ({required}) "
                  f"{child.get('objective')}")


def cmd_goal(args: argparse.Namespace) -> int:
    from remote import relay

    from . import project_arg
    from .remote_task import _resolve_project

    args = project_arg.resolve(args)
    base, token, _label = _resolve(args)
    if args.goal_action == "run":
        project_id = _resolve_project(base, token, args.project)
        agent = getattr(args, "agent", "codex")
        goal = relay.create_goal(
            base, token, project_id=project_id, objective=args.objective,
            done_when=args.done_when, model=args.model, token_budget=args.token_budget,
            tools=_tools(args.tools), name=args.name,
            agents=["codex", "claude"] if agent == "auto" else [agent],
            required_capabilities=getattr(args, "require", []),
            evals=_evals(getattr(args, "evals", None)),
            allow_subgoals=getattr(args, "allow_subgoals", False))
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
    elif args.goal_action == "evidence":
        print(json.dumps(relay.get_goal_evidence(base, token, args.goal_id), indent=2))
        return 0
    else:
        goal = relay.control_goal(base, token, args.goal_id, args.goal_action)
    _show(goal, args.json)
    return 0
