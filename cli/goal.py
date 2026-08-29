"""Remote ``grid goal`` surface over the relay's durable Goal conversations."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from itertools import pairwise
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
            merge = f" · fan-in {child['merge_state']}" if child.get("merge_state") else ""
            print(f"    {child.get('id')} [{child.get('status')}] ({required}) "
                  f"{child.get('objective')}{merge}")
            if child.get("merge_error"):
                print(f"      {child['merge_error']}")


def _verify_evidence(record: dict, *, min_execution_nodes: int = 1,
                     require_inference: bool = False) -> list[str]:
    """Return deterministic release-gate failures in a relay-authored Goal evidence record."""
    failures: list[str] = []
    if record.get("schema_version") != 1:
        failures.append(
            f"unsupported Goal evidence schema version {record.get('schema_version')!r}")
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

    attempt_events = (record.get("attempt_events")
                      if isinstance(record.get("attempt_events"), list) else [])

    # Tool events are durable training evidence, so their worker/attempt boundary must be explicit
    # and relay-authenticated. Sequence order cannot recover that boundary after a dead provider's
    # lease is reclaimed by another machine on the same turn.
    tool_types = {
        f"goal.{mode}.{phase}"
        for mode in ("observe", "act", "verify")
        for phase in ("request", "result")
    }
    tool_events: list[tuple[str, str, str, int, str, str, str | None]] = []
    for index, item in enumerate(attempt_events, 1):
        if not isinstance(item, dict) or not isinstance(item.get("event"), dict):
            continue
        event = item["event"]
        event_type = event.get("type")
        if event_type not in tool_types:
            continue
        turn_id = item.get("turn_id")
        provider_node_id = event.get("provider_node_id")
        attempt = event.get("attempt")
        tool = event.get("tool")
        call_id = event.get("call_id")
        valid = True
        for field, value in (
                ("turn_id", turn_id), ("provider_node_id", provider_node_id),
                ("tool", tool), ("call_id", call_id)):
            if not isinstance(value, str) or not value:
                failures.append(f"tool event {index} ({event_type}) has no valid {field}")
                valid = False
        if (not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0):
            failures.append(f"tool event {index} ({event_type}) has no valid attempt")
            valid = False
        key = event.get("idempotency_key") if event_type.startswith("goal.act.") else None
        if event_type.startswith("goal.act.") and (
                not isinstance(key, str)
                or re.fullmatch(r"grid-goal-[0-9a-f]{64}", key) is None):
            failures.append(
                f"tool event {index} ({event_type}) has no valid idempotency_key")
            valid = False
        if valid:
            tool_events.append((
                event_type, turn_id, provider_node_id, attempt, tool, call_id, key))

    safe_requests = {
        (event_type.removesuffix(".request"), turn_id, provider, attempt, tool, call_id)
        for event_type, turn_id, provider, attempt, tool, call_id, _key in tool_events
        if event_type in ("goal.observe.request", "goal.verify.request")
    }
    for event_type, turn_id, provider, attempt, tool, call_id, _key in tool_events:
        if event_type not in ("goal.observe.result", "goal.verify.result"):
            continue
        identity = (
            event_type.removesuffix(".result"), turn_id, provider, attempt, tool, call_id)
        if identity not in safe_requests:
            failures.append(
                f"{event_type} for {tool}/{call_id} has no matching request on the same attempt")

    action_requests = {
        (turn_id, provider, attempt, tool, call_id, key)
        for event_type, turn_id, provider, attempt, tool, call_id, key in tool_events
        if event_type == "goal.act.request"
    }
    action_results = {
        (turn_id, provider, attempt, tool, call_id, key)
        for event_type, turn_id, provider, attempt, tool, call_id, key in tool_events
        if event_type == "goal.act.result"
    }
    result_keys = {identity[-1] for identity in action_results}
    for identity in sorted(action_results):
        if identity not in action_requests:
            _turn, _provider, _attempt, tool, call_id, _key = identity
            failures.append(
                f"goal.act.result for {tool}/{call_id} has no matching request on the same attempt")
    for identity in sorted(action_requests):
        if identity not in action_results and identity[-1] not in result_keys:
            _turn, _provider, _attempt, tool, call_id, _key = identity
            failures.append(
                f"goal.act.request for {tool}/{call_id} has no durable result or idempotent "
                "reconciliation")

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

    execution_nodes = {
        turn.get("provider_node_id") for turn in turns
        if isinstance(turn, dict) and turn.get("provider_node_id")
    }
    # A killed provider cannot appear as the terminal provider on the reclaimed row. The relay's
    # `task.retry` event is the authority that it executed the prior attempt, so include it in the
    # physical-node inventory; provider-authored attempt-start events are deliberately insufficient.
    execution_nodes.update(
        item["event"].get("previous_provider_id")
        for item in attempt_events
        if isinstance(item, dict) and isinstance(item.get("event"), dict)
        and item["event"].get("type") == "task.retry"
        and item["event"].get("previous_provider_id")
    )
    if len(execution_nodes) < min_execution_nodes:
        failures.append(
            f"Goal used {len(execution_nodes)} distinct execution node(s), fewer than required "
            f"{min_execution_nodes}")

    first = turns[0] if isinstance(turns[0], dict) else {}
    if first.get("transcript_commit") is not None:
        failures.append("turn 1 unexpectedly resumes a pre-existing transcript")
    for index, (previous, current) in enumerate(pairwise(turns), 2):
        if not isinstance(previous, dict) or not isinstance(current, dict):
            continue
        if current.get("transcript_commit") != previous.get("transcript_result_commit"):
            failures.append(
                f"turn {index} transcript input does not equal turn {index - 1} output")

    worktree_chain = (trajectory.get("worktree_chain")
                      if isinstance(trajectory.get("worktree_chain"), list) else [])
    retry_checkpoint_chain = (
        trajectory.get("retry_checkpoint_chain")
        if isinstance(trajectory.get("retry_checkpoint_chain"), list) else [])
    for index, (previous, current) in enumerate(pairwise(turns), 2):
        if not isinstance(previous, dict) or not isinstance(current, dict):
            continue
        matching = [edge for edge in worktree_chain if isinstance(edge, dict)
                    and edge.get("from_turn_id") == previous.get("id")
                    and edge.get("to_turn_id") == current.get("id")
                    and edge.get("result_commit") == previous.get("result_commit")
                    and edge.get("input_commit") == current.get("input_commit")]
        if not matching:
            failures.append(
                f"turn {index} has no relay-authored worktree ancestry check from turn "
                f"{index - 1}")
        elif matching[-1].get("ancestor") is not True:
            detail = matching[-1].get("error") or "the commits are unrelated"
            failures.append(
                f"turn {index} input does not contain turn {index - 1} result: {detail}")

    evals = goal.get("evals") if isinstance(goal.get("evals"), list) else []
    runs = record.get("eval_runs") if isinstance(record.get("eval_runs"), list) else []
    inference = record.get("inference") if isinstance(record.get("inference"), list) else []
    requested_model = goal.get("model")
    routed_model = (isinstance(requested_model, str)
                    and (requested_model == "auto" or requested_model.startswith("auto/")))
    turn_ids = {turn.get("id") for turn in turns if isinstance(turn, dict)}
    valid_inference_identity: set[int] = set()
    for index, item in enumerate(inference, 1):
        if not isinstance(item, dict) or item.get("turn_id") not in turn_ids:
            continue
        attempt = item.get("goal_attempt")
        executor = item.get("goal_executor_node_id")
        harness = item.get("goal_agent_kind")
        valid = True
        if (not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0):
            failures.append(f"inference record {index} has no valid Goal attempt")
            valid = False
        if not isinstance(executor, str) or not executor:
            failures.append(f"inference record {index} has no Goal execution node")
            valid = False
        if harness not in ("codex", "claude"):
            failures.append(f"inference record {index} has no valid Goal harness")
            valid = False
        starts = [event["event"] for event in attempt_events
                  if isinstance(event, dict) and event.get("turn_id") == item.get("turn_id")
                  and isinstance(event.get("event"), dict)
                  and event["event"].get("type") == "task.attempt_started"
                  and event["event"].get("attempt") == attempt]
        if valid and not any(
                start.get("provider_id") == executor
                and start.get("agent_kind") == harness for start in starts):
            failures.append(
                f"inference record {index} has no matching relay-stamped attempt identity")
            valid = False
        if valid:
            valid_inference_identity.add(index)
    if isinstance(requested_model, str) and requested_model and not routed_model:
        for item in inference:
            if (isinstance(item, dict) and item.get("turn_id") in turn_ids
                    and item.get("model") != requested_model):
                failures.append(
                    f"turn {item.get('turn_id')} used Grid model {item.get('model')!r}, not the "
                    f"Goal's requested model {requested_model!r}")
    if require_inference:
        for index, turn in enumerate(turns, 1):
            if not isinstance(turn, dict):
                continue
            usage = [item for item_index, item in enumerate(inference, 1)
                     if item_index in valid_inference_identity
                     and item.get("turn_id") == turn.get("id")
                    and isinstance(item.get("requests"), int)
                    and not isinstance(item.get("requests"), bool)
                    and item["requests"] > 0
                    and item.get("state") == "completed"
                    and item.get("model") and item.get("provider_node_id")
                    and (not requested_model or routed_model
                         or item.get("model") == requested_model)]
            if not usage:
                failures.append(
                    f"turn {index} has no model requests attributed to a Grid inference node")
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
    all_retries = [item for item in attempt_events if isinstance(item, dict)
                   and isinstance(item.get("event"), dict)
                   and item["event"].get("type") == "task.retry"]
    for retry in all_retries:
        event = retry["event"]
        previous_agent = event.get("previous_agent_kind")
        if previous_agent not in ("codex", "claude"):
            failures.append(
                f"turn {retry.get('turn_id')} retry attempt {event.get('attempt')} has no "
                "relay-authored previous harness identity")
        starts = [item["event"] for item in attempt_events if isinstance(item, dict)
                  and item.get("turn_id") == retry.get("turn_id")
                  and isinstance(item.get("event"), dict)
                  and item["event"].get("type") == "task.attempt_started"
                  and item["event"].get("attempt") == event.get("attempt")]
        for start in starts:
            if (start.get("provider_id") != event.get("previous_provider_id")
                    or start.get("agent_kind") != previous_agent):
                failures.append(
                    f"turn {retry.get('turn_id')} retry attempt {event.get('attempt')} "
                    "disagrees with its relay-stamped attempt start identity")
    native_retries = [item for item in attempt_events if isinstance(item, dict)
                      and isinstance(item.get("event"), dict)
                      and item["event"].get("type") == "task.retry"
                      and item["event"].get("reason") == "native_harness_failure"
                      and item["event"].get("checkpoint_commit")]
    for retry in native_retries:
        event = retry["event"]
        matching = [check for check in retry_checkpoint_chain if isinstance(check, dict)
                    and check.get("turn_id") == retry.get("turn_id")
                    and check.get("event_seq") == retry.get("seq")
                    and check.get("checkpoint_commit") == event.get("checkpoint_commit")
                    and check.get("transcript_checkpoint_commit")
                    == event.get("transcript_checkpoint_commit")]
        if not matching:
            failures.append(
                f"turn {retry.get('turn_id')} native retry has no relay-authored checkpoint "
                "ancestry proof")
            continue
        check = matching[-1]
        if check.get("worktree_ancestor") is not True:
            failures.append(
                f"turn {retry.get('turn_id')} final worktree does not contain its accepted retry "
                f"checkpoint: {check.get('worktree_error') or 'the commits are unrelated'}")
        if check.get("transcript_ancestor") is not True:
            failures.append(
                f"turn {retry.get('turn_id')} final transcript does not contain its accepted "
                f"retry checkpoint: {check.get('transcript_error') or 'the commits are unrelated'}")
    turns_by_id = {turn.get("id"): turn for turn in turns if isinstance(turn, dict)}
    for turn_id in {retry.get("turn_id") for retry in native_retries}:
        retries = sorted(
            (retry for retry in native_retries if retry.get("turn_id") == turn_id),
            key=lambda item: item.get("seq") if isinstance(item.get("seq"), int) else -1)
        latest = retries[-1]["event"]
        turn = turns_by_id.get(turn_id) or {}
        if turn.get("checkpoint_commit") != latest.get("checkpoint_commit"):
            failures.append(
                f"turn {turn_id} stored worktree checkpoint does not equal its latest accepted "
                "native retry pin")
        if (turn.get("transcript_checkpoint_commit")
                != latest.get("transcript_checkpoint_commit")):
            failures.append(
                f"turn {turn_id} stored transcript checkpoint does not equal its latest accepted "
                "native retry pin")
    final_turn = turns[-1] if isinstance(turns[-1], dict) else {}
    final_turn_id = final_turn.get("id")
    final_commit = final_turn.get("result_commit")
    for spec in evals:
        if not isinstance(spec, dict):
            failures.append("Goal contains a malformed evaluation definition")
            continue
        definition_id = spec.get("definition_id")
        definition_hash = spec.get("definition_hash")
        if not definition_id or not definition_hash:
            failures.append(
                f"evaluation {spec.get('name') or '?'} has no immutable definition id and hash")
            continue
        definition_body = {
            key: value for key, value in spec.items()
            if key not in {"definition_id", "definition_hash"}
        }
        encoded_definition = json.dumps(
            definition_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        actual_hash = hashlib.sha256(encoded_definition.encode("utf-8")).hexdigest()
        if actual_hash != definition_hash:
            failures.append(
                f"evaluation {spec.get('name') or definition_id} body does not match its "
                "immutable definition hash")
            continue
        # File v1 is executed by deterministic relay code, never by the acting harness. A random
        # nonempty node name is not proof of independent evaluation in a release artifact.
        expected_evaluator = "relay" if spec.get("type") == "file" else None
        accepted = [run for run in runs if isinstance(run, dict)
                    and run.get("accepted") is True and run.get("passed") is True
                    and run.get("state") == "passed"
                    and run.get("turn_id") == final_turn_id
                    and run.get("result_commit") == final_commit
                    and run.get("definition_id") == definition_id
                    and run.get("definition_hash") == definition_hash
                    and run.get("evaluator_node_id")
                    and (expected_evaluator is None
                         or run.get("evaluator_node_id") == expected_evaluator)
                    and run.get("accepted_at")
                    and isinstance(run.get("score"), (int, float))
                    and not isinstance(run.get("score"), bool)
                    and 0 <= run["score"] <= 1]
        if not accepted:
            failures.append(
                f"evaluation {spec.get('name') or definition_id or definition_hash or '?'} has no "
                "accepted passing run from the final turn for the final result commit")
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
            failures = _verify_evidence(
                evidence,
                min_execution_nodes=getattr(args, "min_execution_nodes", 1),
                require_inference=getattr(args, "require_inference", False))
            if failures:
                raise SystemExit("Goal evidence verification failed:\n- " + "\n- ".join(failures))
            print("Goal evidence verified: terminal turns, transcript chain and evaluations pass.",
                  file=sys.stderr)
        return 0
    else:
        goal = relay.control_goal(base, token, args.goal_id, args.goal_action)
    _show(goal, args.json)
    return 0
