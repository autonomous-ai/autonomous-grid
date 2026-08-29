"""Remote ``grid goal`` surface over the relay's durable Goal conversations."""
from __future__ import annotations

import argparse
import json
import sys
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


def _verify_evidence(record: dict) -> list[str]:
    """Return deterministic release-gate failures in a relay-authored Goal evidence record."""
    failures: list[str] = []
    goal = record.get("goal") if isinstance(record.get("goal"), dict) else {}
    turns = record.get("turns") if isinstance(record.get("turns"), list) else []
    if goal.get("status") != "complete":
        failures.append(f"Goal status is {goal.get('status')!r}, not 'complete'")
    if not turns:
        failures.append("no Goal turns were recorded")
        return failures

    trajectory = (record.get("trajectory")
                  if isinstance(record.get("trajectory"), dict) else {})
    if trajectory.get("transcript_pruned") is True:
        failures.append("the Goal transcript ref has been pruned")
    pruned_branches = trajectory.get("pruned_turn_branches")
    if isinstance(pruned_branches, list) and pruned_branches:
        failures.append(
            "turn branches have been pruned: " + ", ".join(map(str, pruned_branches)))

    for index, turn in enumerate(turns, 1):
        if not isinstance(turn, dict):
            failures.append(f"turn {index} is not an object")
            continue
        if turn.get("state") != "completed":
            failures.append(f"turn {index} state is {turn.get('state')!r}, not 'completed'")
        if turn.get("branch_pruned") is True:
            failures.append(f"turn {index} result branch has been pruned")
        for field in ("agent_kind", "provider_node_id", "input_commit", "result_commit"):
            if not turn.get(field):
                failures.append(f"turn {index} has no {field}")
        if not turn.get("transcript_result_commit"):
            failures.append(f"turn {index} has no verified transcript output commit")

    first = turns[0] if isinstance(turns[0], dict) else {}
    if first.get("transcript_commit") is not None:
        failures.append("turn 1 unexpectedly resumes a pre-existing transcript")
    for index, (previous, current) in enumerate(zip(turns, turns[1:]), 2):
        if not isinstance(previous, dict) or not isinstance(current, dict):
            continue
        if current.get("transcript_commit") != previous.get("transcript_result_commit"):
            failures.append(
                f"turn {index} transcript input does not equal turn {index - 1} output")

    evals = goal.get("evals") if isinstance(goal.get("evals"), list) else []
    runs = record.get("eval_runs") if isinstance(record.get("eval_runs"), list) else []
    attempt_events = (record.get("attempt_events")
                      if isinstance(record.get("attempt_events"), list) else [])
    for index, turn in enumerate(turns, 1):
        if not isinstance(turn, dict):
            continue
        attempt = turn.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 1:
            continue
        retry = [item for item in attempt_events if isinstance(item, dict)
                 and item.get("turn_id") == turn.get("id")
                 and isinstance(item.get("event"), dict)
                 and item["event"].get("type") == "task.retry"
                 and item["event"].get("attempt") == attempt - 1
                 and item["event"].get("previous_provider_id")]
        if not retry:
            failures.append(
                f"turn {index} attempt {attempt} has no authoritative retry event naming the "
                "previous provider")
    final_commit = turns[-1].get("result_commit") if isinstance(turns[-1], dict) else None
    for spec in evals:
        if not isinstance(spec, dict):
            failures.append("Goal contains a malformed evaluation definition")
            continue
        definition_id = spec.get("definition_id")
        definition_hash = spec.get("definition_hash")
        accepted = [run for run in runs if isinstance(run, dict)
                    and run.get("accepted") is True and run.get("passed") is True
                    and run.get("result_commit") == final_commit
                    and ((definition_id and run.get("definition_id") == definition_id)
                         or (definition_hash
                             and run.get("definition_hash") == definition_hash))]
        if not accepted:
            failures.append(
                f"evaluation {spec.get('name') or definition_id or definition_hash or '?'} has no "
                "accepted passing run for the final result commit")
    return failures


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
        evidence = relay.get_goal_evidence(base, token, args.goal_id)
        print(json.dumps(evidence, indent=2))
        if getattr(args, "verify", False):
            failures = _verify_evidence(evidence)
            if failures:
                raise SystemExit("Goal evidence verification failed:\n- " + "\n- ".join(failures))
            print("Goal evidence verified: terminal turns, transcript chain and evaluations pass.",
                  file=sys.stderr)
        return 0
    else:
        goal = relay.control_goal(base, token, args.goal_id, args.goal_action)
    _show(goal, args.json)
    return 0
