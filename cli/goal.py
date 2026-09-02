"""Remote ``grid goal`` surface over the relay's durable Goal conversations."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import uuid
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


_MISSING = object()


def _json_number(value: object) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and (not isinstance(value, float) or math.isfinite(value)))


def _json_equal(left: object, right: object) -> bool:
    """JSON equality without Python's nested True == 1 coercion."""
    pending = [(left, right)]
    while pending:
        left, right = pending.pop()
        if isinstance(left, bool) or isinstance(right, bool):
            if type(left) is not type(right) or left != right:
                return False
        elif _json_number(left) and _json_number(right):
            if left != right:
                return False
        elif type(left) is not type(right):
            return False
        elif isinstance(left, dict):
            if left.keys() != right.keys():
                return False
            pending.extend((left[key], right[key]) for key in left)
        elif isinstance(left, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right))
        elif left != right:
            return False
    return True


def _json_pointer(document: object, pointer: object) -> object:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        return _MISSING
    current = document
    if pointer == "":
        return current
    for encoded in pointer.split("/")[1:]:
        if re.search(r"~(?:[^01]|$)", encoded):
            return _MISSING
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return _MISSING
            current = current[token]
        elif isinstance(current, list):
            if (not token.isascii() or not token.isdigit()
                    or (len(token) > 1 and token.startswith("0"))):
                return _MISSING
            index = int(token)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _json_checks_pass(document: object, checks: object) -> bool:
    if not isinstance(checks, list) or not checks:
        return False
    for check in checks:
        if not isinstance(check, dict):
            return False
        pointer, op, expected = (
            check.get("pointer"), check.get("op"), check.get("value", _MISSING))
        if expected is _MISSING:
            return False
        actual = _json_pointer(document, pointer)
        if op == "exists":
            passed = isinstance(expected, bool) and ((actual is not _MISSING) is expected)
        elif actual is _MISSING:
            passed = False
        elif op == "equals":
            passed = _json_equal(actual, expected)
        elif op == "not_equals":
            passed = not _json_equal(actual, expected)
        elif op == "greater_or_equal":
            passed = _json_number(actual) and _json_number(expected) and actual >= expected
        elif op == "less_or_equal":
            passed = _json_number(actual) and _json_number(expected) and actual <= expected
        else:
            passed = False
        if not passed:
            return False
    return True


def _verify_eval_events(spec: dict, run: dict, attempt_events: list,
                        final_turn: dict) -> list[str]:
    """Reproduce a verify metric from the exported final-attempt request/result pair."""
    label = spec.get("name") or spec.get("definition_id") or "?"
    evidence = run.get("evidence")
    if not isinstance(evidence, dict):
        return [f"verify evaluation {label} has no structured relay evidence"]
    provider = final_turn.get("provider_node_id")
    attempt = final_turn.get("attempt")
    tool = spec.get("tool")
    arguments = spec.get("arguments")
    if (not isinstance(provider, str) or not provider
            or not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0):
        return [f"verify evaluation {label} has no valid final-attempt identity"]
    if (evidence.get("provider_node_id") != provider or evidence.get("attempt") != attempt
            or evidence.get("tool") != tool):
        return [f"verify evaluation {label} evidence is not tied to the final leased attempt"]

    requests = []
    for item in attempt_events:
        event = item.get("event") if isinstance(item, dict) else None
        seq = item.get("seq") if isinstance(item, dict) else None
        if (isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0
                and item.get("turn_id") == final_turn.get("id")
                and isinstance(event, dict)
                and event.get("type") == "goal.verify.request"
                and event.get("provider_node_id") == provider
                and event.get("attempt") == attempt
                and event.get("tool") == tool
                and _json_equal(event.get("arguments", _MISSING), arguments)
                and isinstance(event.get("call_id"), str) and event["call_id"]):
            requests.append((seq, event))
    if not requests:
        return [f"verify evaluation {label} has no matching exported final-attempt request"]
    if len({seq for seq, _event in requests}) != len(requests):
        return [f"verify evaluation {label} has ambiguous duplicate request sequences"]
    requests.sort(key=lambda item: item[0])
    request_seq, request = requests[-1]
    call_id = request["call_id"]
    if (evidence.get("request_seq") != request_seq
            or evidence.get("call_id") != call_id):
        return [f"verify evaluation {label} does not name the final matching request"]

    results = []
    for item in attempt_events:
        event = item.get("event") if isinstance(item, dict) else None
        seq = item.get("seq") if isinstance(item, dict) else None
        if (isinstance(seq, int) and not isinstance(seq, bool) and seq > request_seq
                and item.get("turn_id") == final_turn.get("id")
                and isinstance(event, dict)
                and event.get("type") == "goal.verify.result"
                and event.get("provider_node_id") == provider
                and event.get("attempt") == attempt
                and event.get("tool") == tool and event.get("call_id") == call_id):
            results.append((seq, event))
    if not results:
        return [f"verify evaluation {label} has no matching exported result"]
    if len({seq for seq, _event in results}) != len(results):
        return [f"verify evaluation {label} has ambiguous duplicate result sequences"]
    results.sort(key=lambda item: item[0])
    result_seq, result_event = results[-1]
    if evidence.get("result_seq") != result_seq:
        return [f"verify evaluation {label} does not name the final matching result"]
    result = result_event.get("result")
    if result_event.get("success") is not True or not isinstance(result, dict):
        return [f"verify evaluation {label} exported result was not successful"]
    if not _json_checks_pass(result, spec.get("checks")):
        return [f"verify evaluation {label} does not pass when recomputed from exported events"]
    return []


def _show(goal: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(goal, indent=2))
        return
    print(f"{goal.get('id')} [{goal.get('status')}]")
    print(f"  objective  {goal.get('objective')}")
    print(f"  done when  {goal.get('done_when')}")
    model = goal.get("model")
    readiness = (goal.get("model_readiness")
                 if isinstance(goal.get("model_readiness"), dict) else {})
    model_suffix = ""
    if readiness.get("state") == "waiting":
        model_suffix = " · waiting for compatible Grid inference"
    elif readiness.get("state") == "ready" and readiness.get("agents"):
        model_suffix = f" · ready via {', '.join(readiness['agents'])}"
    if model:
        print(f"  model      {model}{model_suffix}")
    print(f"  progress   {goal.get('turns_completed', 0)} turns · "
          f"{goal.get('tokens_used', 0)} tokens")
    if goal.get("token_budget") is not None:
        reserved = int(goal.get("child_tokens_reserved") or 0)
        descendants = int(goal.get("descendant_tokens_used") or 0)
        details = []
        if descendants:
            details.append(f"{descendants:,} used by descendants")
        if reserved:
            details.append(f"{reserved:,} reserved for live children")
        suffix = " · " + " · ".join(details) if details else ""
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
                     require_inference: bool = False,
                     require_worker_revision: str | None = None,
                     require_agent_sequence: tuple[str, ...] | None = None) -> list[str]:
    """Return deterministic release-gate failures in a relay-authored Goal evidence record."""
    failures: list[str] = []
    schema_version = record.get("schema_version")
    if (not isinstance(schema_version, int) or isinstance(schema_version, bool)
            or schema_version != 1):
        failures.append(
            f"unsupported Goal evidence schema version {schema_version!r}")
    raw_goal = record.get("goal")
    if not isinstance(raw_goal, dict):
        failures.append("Goal evidence has no valid goal object")
    goal = raw_goal if isinstance(raw_goal, dict) else {}
    raw_turns = record.get("turns")
    if not isinstance(raw_turns, list):
        failures.append("Goal evidence has no valid turns list")
    turns = raw_turns if isinstance(raw_turns, list) else []
    if goal.get("status") != "complete":
        failures.append(f"Goal status is {goal.get('status')!r}, not 'complete'")
    progress = goal.get("turns_completed")
    if progress is not None:
        if (not isinstance(progress, int) or isinstance(progress, bool) or progress < 0):
            failures.append("Goal has no valid nonnegative turns_completed")
        elif progress != len(turns):
            failures.append(
                "Goal completed-turn counter does not equal its accepted turn trajectory")

    # New relays separate native-parent usage from usage charged by terminal descendants. This is
    # training/release evidence, so verify the arithmetic rather than trusting the displayed total.
    budget_fields = (
        "tokens_used", "own_tokens_used", "descendant_tokens_used", "child_tokens_reserved")
    if "own_tokens_used" in goal or "descendant_tokens_used" in goal:
        values = {field: goal.get(field) for field in budget_fields}
        for field, value in values.items():
            if (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                failures.append(f"Goal has no valid nonnegative {field}")
        if all(isinstance(values[field], int) and not isinstance(values[field], bool)
               and values[field] >= 0 for field in budget_fields):
            if (values["tokens_used"]
                    != values["own_tokens_used"] + values["descendant_tokens_used"]):
                failures.append(
                    "Goal total token usage does not equal own plus descendant usage")
            relationships = (record.get("relationships")
                             if isinstance(record.get("relationships"), dict) else {})
            children = (relationships.get("children")
                        if isinstance(relationships.get("children"), list) else None)
            if children is not None:
                charged = 0
                reserved = 0
                valid_children = True
                for child in children:
                    if not isinstance(child, dict):
                        failures.append("Goal relationships contain a malformed child")
                        valid_children = False
                        continue
                    allocation = child.get("token_budget")
                    actual = child.get("tokens_charged")
                    if (not isinstance(allocation, int) or isinstance(allocation, bool)
                            or allocation <= 0):
                        failures.append(
                            f"child {child.get('id') or '?'} has no valid token allocation")
                        valid_children = False
                    elif actual is None:
                        reserved += allocation
                        if child.get("status") in (
                                "complete", "failed", "cancelled", "budget_limited",
                                "usage_limited"):
                            failures.append(
                                f"terminal child {child.get('id') or '?'} still has a live token "
                                "reservation")
                            valid_children = False
                    elif (not isinstance(actual, int) or isinstance(actual, bool) or actual < 0):
                        failures.append(
                            f"child {child.get('id') or '?'} has no valid actual token charge")
                        valid_children = False
                    else:
                        charged += actual
                if valid_children and charged != values["descendant_tokens_used"]:
                    failures.append(
                        "Goal descendant token usage does not equal its settled child charges")
                if valid_children and reserved != values["child_tokens_reserved"]:
                    failures.append(
                        "Goal live child token reservations do not equal its unsettled allocations")
    if not turns:
        failures.append("no Goal turns were recorded")
        return failures
    recorded_turn_ids: list[str] = []
    for turn in turns:
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if isinstance(turn_id, str) and turn_id:
            recorded_turn_ids.append(turn_id)
    turn_ids = set(recorded_turn_ids)
    if len(recorded_turn_ids) != len(turns) or len(turn_ids) != len(turns):
        failures.append("Goal evidence contains duplicate or malformed turn identities")
    export = record.get("export")
    if export is not None:
        pages = export.get("pages") if isinstance(export, dict) else None
        total_turns = export.get("total_turns") if isinstance(export, dict) else None
        if (not isinstance(export, dict) or export.get("paginated") is not True
                or not isinstance(pages, int) or isinstance(pages, bool) or pages < 1
                or not isinstance(total_turns, int) or isinstance(total_turns, bool)
                or total_turns != len(turns)):
            failures.append("Goal evidence has inconsistent paginated export metadata")
        snapshot = export.get("snapshot") if isinstance(export, dict) else None
        if (not isinstance(snapshot, str)
                or re.fullmatch(r"[0-9a-f]{64}", snapshot) is None):
            # The client may still READ pagination from the short-lived pre-fingerprint relay for
            # rolling compatibility, but such an export is not strong enough to label training
            # data or pass a release gate: removing the snapshot from an artifact must not weaken
            # verification into accepting pages from different moments.
            failures.append("Goal evidence has no valid paginated snapshot")

    raw_relationships = record.get("relationships")
    if not isinstance(raw_relationships, dict):
        failures.append("Goal evidence has no valid relationships object")
    raw_trajectory = record.get("trajectory")
    if not isinstance(raw_trajectory, dict):
        failures.append("Goal evidence has no valid trajectory object")
    trajectory = raw_trajectory if isinstance(raw_trajectory, dict) else {}
    if trajectory.get("transcript_pruned") is True:
        failures.append("the Goal transcript ref has been pruned")
    pruned_branches = trajectory.get("pruned_turn_branches")
    if isinstance(pruned_branches, list) and pruned_branches:
        failures.append(
            "turn branches have been pruned: " + ", ".join(map(str, pruned_branches)))

    tool_types = {
        f"goal.{mode}.{phase}"
        for mode in ("observe", "act", "verify")
        for phase in ("request", "result")
    }
    native_goal_types = {
        "goal.codex.event", "goal.slice.completed",
        "goal.claude.set", "goal.claude.evaluated", "goal.claude.evaluator_missing",
    }
    worker_goal_types = tool_types | native_goal_types
    exported_event_types = worker_goal_types | {
        "goal.eval.completed",
        "task.attempt_started", "task.claim_expired", "task.retry", "task.retrying",
        "task.cancelled", "task.terminal", "task.event.corrupt",
    }
    raw_attempt_events = record.get("attempt_events")
    if not isinstance(raw_attempt_events, list):
        failures.append("Goal evidence has no valid attempt_events list")
    attempt_events = raw_attempt_events if isinstance(raw_attempt_events, list) else []
    event_coordinates: set[tuple[str, int]] = set()
    last_event_seq: dict[str, int] = {}
    for index, item in enumerate(attempt_events, 1):
        if not isinstance(item, dict):
            failures.append(f"attempt event {index} is not an object")
            continue
        turn_id = item.get("turn_id")
        seq = item.get("seq")
        event = item.get("event")
        if not isinstance(turn_id, str) or turn_id not in turn_ids:
            failures.append(f"attempt event {index} names an unknown Goal turn")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            failures.append(f"attempt event {index} has no valid relay sequence")
        elif isinstance(turn_id, str):
            coordinate = (turn_id, seq)
            if coordinate in event_coordinates:
                failures.append(
                    f"attempt event {index} duplicates relay sequence {seq} for turn {turn_id}")
            else:
                event_coordinates.add(coordinate)
            previous_seq = last_event_seq.get(turn_id)
            if previous_seq is not None and seq <= previous_seq:
                failures.append(
                    f"attempt event {index} is out of relay sequence order for turn {turn_id}")
            last_event_seq[turn_id] = max(seq, previous_seq if previous_seq is not None else seq)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str) or not event["type"]:
            failures.append(f"attempt event {index} has no valid event object and type")
            continue
        if event["type"] not in exported_event_types:
            failures.append(
                f"attempt event {index} has unknown Goal evidence type {event['type']!r}")
        if event.get("type") == "task.event.corrupt":
            failures.append(f"attempt event {index} contains corrupt stored evidence")

    # A turn row and its terminal event are committed in one relay transaction. Require both
    # halves of that invariant in an exported artifact: accepting only the row would let a pruned
    # or contradictory event stream masquerade as complete training evidence. `task.terminal` is
    # relay-only, so it is also the durable proof that the worker's terminal nomination crossed
    # the lease fence exactly once.
    terminal_events: dict[str, list[tuple[int, int, dict]]] = {}
    for index, item in enumerate(attempt_events, 1):
        if (not isinstance(item, dict) or not isinstance(item.get("event"), dict)
                or item["event"].get("type") != "task.terminal"):
            continue
        turn_id = item.get("turn_id")
        seq = item.get("seq")
        if not isinstance(turn_id, str) or turn_id not in turn_ids:
            failures.append(f"terminal event {index} names an unknown Goal turn")
            continue
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            failures.append(f"terminal event {index} has no valid relay sequence")
            continue
        terminal_events.setdefault(turn_id, []).append((index, seq, item["event"]))

    for turn_index, turn in enumerate(turns, 1):
        if not isinstance(turn, dict):
            continue
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or not turn_id:
            continue
        terminals = terminal_events.get(turn_id, [])
        if len(terminals) != 1:
            failures.append(
                f"turn {turn_index} has {len(terminals)} relay terminal events; expected exactly "
                "one")
            continue
        _event_index, terminal_seq, terminal = terminals[0]
        if terminal.get("state") != turn.get("state"):
            failures.append(
                f"turn {turn_index} terminal event state does not match its stored state")
        if terminal.get("error") != turn.get("error"):
            failures.append(
                f"turn {turn_index} terminal event error does not match its stored error")
        later_sequences = [
            item.get("seq") for item in attempt_events
            if (isinstance(item, dict) and item.get("turn_id") == turn_id
                and isinstance(item.get("seq"), int) and not isinstance(item.get("seq"), bool)
                and item.get("seq") > terminal_seq)
        ]
        if later_sequences:
            failures.append(
                f"turn {turn_index} has evidence after its relay terminal event")

    # A native Goal claim can outlive its worker before Codex/Claude crosses the durable start
    # fence. This is useful fleet evidence, but it is neither an execution attempt nor a failed
    # training trajectory. Validate its relay-authored shape without adding its node to the set of
    # workers that actually ran the Goal.
    for index, item in enumerate(attempt_events, 1):
        if (not isinstance(item, dict) or not isinstance(item.get("event"), dict)
                or item["event"].get("type") != "task.claim_expired"):
            continue
        event = item["event"]
        turn_id = item.get("turn_id")
        attempt = event.get("attempt")
        if not isinstance(turn_id, str) or turn_id not in turn_ids:
            failures.append(f"expired claim event {index} names an unknown Goal turn")
        if (not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0):
            failures.append(f"expired claim event {index} has no valid attempt")
        if (event.get("reason") != "lease_expired_before_start"
                or event.get("attempt_reused") is not True):
            failures.append(
                f"expired claim event {index} does not prove a reusable pre-start claim")
        previous_provider = event.get("previous_provider_id")
        previous_agent = event.get("previous_agent_kind")
        if previous_provider is not None and (
                not isinstance(previous_provider, str) or not previous_provider):
            failures.append(f"expired claim event {index} has malformed provider identity")
        if previous_agent is not None and previous_agent not in ("codex", "claude"):
            failures.append(f"expired claim event {index} has malformed harness identity")

    # Tool events are durable training evidence, so their worker/attempt boundary must be explicit
    # and relay-authenticated. Sequence order cannot recover that boundary after a dead provider's
    # lease is reclaimed by another machine on the same turn.
    tool_events: list[tuple[str, str, str, int, str, str, str | None]] = []
    for index, item in enumerate(attempt_events, 1):
        if not isinstance(item, dict) or not isinstance(item.get("event"), dict):
            continue
        event = item["event"]
        event_type = event.get("type")
        if not isinstance(event_type, str):
            failures.append(f"attempt event {index} has no valid event type")
            continue
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
        attempt = turn.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0:
            failures.append(f"turn {index} has no valid terminal attempt")
        if not turn.get("transcript_result_commit"):
            failures.append(f"turn {index} has no verified transcript output commit")

    turns_by_id = {
        turn["id"]: turn for turn in turns
        if isinstance(turn, dict) and isinstance(turn.get("id"), str) and turn.get("id")}
    starts_by_attempt: dict[tuple[str, int], list[dict]] = {}
    start_sequences_by_attempt: dict[tuple[str, int], list[int]] = {}
    turn_positions = {turn_id: index for index, turn_id in enumerate(recorded_turn_ids)}
    ordered_attempt_starts: list[tuple[int, int, int, str]] = []
    for index, item in enumerate(attempt_events, 1):
        if (not isinstance(item, dict) or not isinstance(item.get("event"), dict)
                or item["event"].get("type") != "task.attempt_started"):
            continue
        event = item["event"]
        turn_id = item.get("turn_id")
        attempt = event.get("attempt")
        valid_coordinate = True
        if not isinstance(turn_id, str) or turn_id not in turns_by_id:
            failures.append(f"attempt start event {index} names an unknown Goal turn")
            valid_coordinate = False
        if (not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0):
            failures.append(f"attempt start event {index} has no valid attempt")
            valid_coordinate = False
        provider = event.get("provider_id")
        harness = event.get("agent_kind")
        if not isinstance(provider, str) or not provider:
            failures.append(f"attempt start event {index} has no provider identity")
        if harness not in ("codex", "claude"):
            failures.append(f"attempt start event {index} has no valid harness identity")
        if valid_coordinate:
            coordinate = (turn_id, attempt)
            starts_by_attempt.setdefault(coordinate, []).append(event)
            seq = item.get("seq")
            if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0:
                start_sequences_by_attempt.setdefault(coordinate, []).append(seq)
                if (isinstance(provider, str) and provider
                        and harness in ("codex", "claude")):
                    ordered_attempt_starts.append(
                        (turn_positions[turn_id], seq, attempt, harness))
            terminal_attempt = turns_by_id[turn_id].get("attempt")
            if (isinstance(terminal_attempt, int) and not isinstance(terminal_attempt, bool)
                    and terminal_attempt > 0 and attempt > terminal_attempt):
                failures.append(
                    f"turn {turn_id} has an attempt start after terminal attempt "
                    f"{terminal_attempt}")

    # The terminal row names the worker whose result the relay accepted. Require exactly one
    # relay-stamped start fence for that same attempt and identity; without it, a completed row is
    # not proof that Codex or Claude ever ran, and must not become Goal training evidence.
    for index, turn in enumerate(turns, 1):
        if not isinstance(turn, dict):
            continue
        turn_id = turn.get("id")
        attempt = turn.get("attempt")
        if (not isinstance(turn_id, str) or turn_id not in turns_by_id
                or not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0):
            continue
        starts = starts_by_attempt.get((turn_id, attempt), [])
        matching = [
            start for start in starts
            if start.get("provider_id") == turn.get("provider_node_id")
            and start.get("agent_kind") == turn.get("agent_kind")
        ]
        if len(starts) != 1 or len(matching) != 1:
            failures.append(
                f"turn {index} terminal attempt {attempt} has {len(starts)} relay-stamped start "
                "records; expected exactly one matching its provider and harness")

    execution_nodes = {
        turn.get("provider_node_id") for turn in turns
        if (isinstance(turn, dict)
            and isinstance(turn.get("provider_node_id"), str)
            and turn.get("provider_node_id"))
    }

    if require_agent_sequence:
        observed_agents = [item[-1] for item in sorted(ordered_attempt_starts)]
        required_index = 0
        for agent in observed_agents:
            if agent == require_agent_sequence[required_index]:
                required_index += 1
                if required_index == len(require_agent_sequence):
                    break
        if required_index != len(require_agent_sequence):
            failures.append(
                "relay-stamped execution agent sequence "
                f"{','.join(observed_agents) or '(none)'} does not contain required ordered "
                f"sequence {','.join(require_agent_sequence)}")

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
    if len(worktree_chain) != max(0, len(turns) - 1):
        failures.append(
            "Goal worktree ancestry chain does not contain exactly one edge per turn handoff")
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

    raw_evals = goal.get("evals")
    if not isinstance(raw_evals, list):
        failures.append("Goal evidence has no valid immutable eval list")
    evals = raw_evals if isinstance(raw_evals, list) else []
    raw_runs = record.get("eval_runs")
    if not isinstance(raw_runs, list):
        failures.append("Goal evidence has no valid eval_runs list")
    runs = raw_runs if isinstance(raw_runs, list) else []
    for run in runs:
        if (isinstance(run, dict) and isinstance(run.get("evidence"), dict)
                and run["evidence"].get("_corrupt") is True):
            failures.append(
                f"evaluation run {run.get('id') or '?'} contains corrupt stored evidence")
    raw_inference = record.get("inference")
    if not isinstance(raw_inference, list):
        failures.append("Goal evidence has no valid inference list")
    inference = raw_inference if isinstance(raw_inference, list) else []
    requested_model = goal.get("model")
    routed_model = (isinstance(requested_model, str)
                    and (requested_model == "auto" or requested_model.startswith("auto/")))
    valid_inference_identity: set[int] = set()
    for index, item in enumerate(inference, 1):
        inference_turn_id = item.get("turn_id") if isinstance(item, dict) else None
        if (not isinstance(inference_turn_id, str)
                or inference_turn_id not in turn_ids):
            failures.append(f"inference record {index} names an unknown or malformed Goal turn")
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
            item_turn_id = item.get("turn_id") if isinstance(item, dict) else None
            if (isinstance(item_turn_id, str) and item_turn_id in turn_ids
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
        for prior_attempt in range(1, attempt):
            retry = [item for item in attempt_events if isinstance(item, dict)
                     and item.get("turn_id") == turn.get("id")
                     and isinstance(item.get("event"), dict)
                     and item["event"].get("type") == "task.retry"
                     and item["event"].get("attempt") == prior_attempt
                     and item["event"].get("previous_provider_id")]
            if not retry:
                failures.append(
                    f"turn {index} attempt {attempt} has no authoritative retry event naming the "
                    f"provider for prior attempt {prior_attempt}")
            elif len(retry) != 1:
                failures.append(
                    f"turn {index} attempt {attempt} has {len(retry)} authoritative retry events "
                    f"for prior attempt {prior_attempt}; expected exactly one")
    all_retries = [item for item in attempt_events if isinstance(item, dict)
                   and isinstance(item.get("event"), dict)
                   and item["event"].get("type") == "task.retry"]
    verified_retry_nodes: set[str] = set()
    verified_retry_attempts: set[tuple[str, int, str, str]] = set()
    retry_sequences_by_attempt: dict[tuple[str, int], list[int]] = {}
    for retry in all_retries:
        event = retry["event"]
        turn_id = retry.get("turn_id")
        attempt = event.get("attempt")
        retry_seq = retry.get("seq")
        previous_provider = event.get("previous_provider_id")
        previous_agent = event.get("previous_agent_kind")
        valid = True
        if not isinstance(turn_id, str) or turn_id not in turn_ids:
            failures.append(f"retry event names unknown turn {turn_id!r}")
            valid = False
        if (not isinstance(attempt, int) or isinstance(attempt, bool) or attempt <= 0):
            failures.append(f"turn {turn_id} retry has no valid prior attempt")
            valid = False
        if not isinstance(previous_provider, str) or not previous_provider:
            failures.append(
                f"turn {turn_id} retry attempt {attempt} has no previous provider identity")
            valid = False
        if previous_agent not in ("codex", "claude"):
            failures.append(
                f"turn {turn_id} retry attempt {attempt} has no "
                "relay-authored previous harness identity")
            valid = False
        starts = starts_by_attempt.get((turn_id, attempt), [])
        matching_starts = [
            start for start in starts
            if start.get("provider_id") == previous_provider
            and start.get("agent_kind") == previous_agent
        ]
        if len(matching_starts) != 1 or len(starts) != 1:
            failures.append(
                f"turn {turn_id} retry attempt {attempt} disagrees with its relay-stamped "
                "attempt start identity")
            valid = False
        if (isinstance(turn_id, str) and isinstance(attempt, int)
                and not isinstance(attempt, bool) and attempt > 0
                and isinstance(retry_seq, int) and not isinstance(retry_seq, bool)
                and retry_seq >= 0):
            coordinate = (turn_id, attempt)
            retry_sequences_by_attempt.setdefault(coordinate, []).append(retry_seq)
            start_sequences = start_sequences_by_attempt.get(coordinate, [])
            if len(start_sequences) == 1 and retry_seq <= start_sequences[0]:
                failures.append(
                    f"turn {turn_id} retry attempt {attempt} is not after its attempt start")
                valid = False
            next_start_sequences = start_sequences_by_attempt.get((turn_id, attempt + 1), [])
            if len(next_start_sequences) == 1 and retry_seq >= next_start_sequences[0]:
                failures.append(
                    f"turn {turn_id} next attempt start is not after retry attempt {attempt}")
                valid = False
        if valid and len(matching_starts) == 1:
            verified_retry_nodes.add(previous_provider)
            verified_retry_attempts.add((
                turn_id, attempt, previous_provider, previous_agent))

    # Every worker-authored Goal event belongs strictly inside one live attempt. The relay stamps
    # provider and attempt onto these events, so require them to agree with the unique start fence.
    # This covers native Codex/Claude Goal progress as well as business tool traffic, and rejects
    # impossible histories before start or after the relay declared the attempt dead and reusable.
    native_verdicts: dict[tuple[str, int, str, str], list[tuple[int, dict]]] = {}
    claude_sets: dict[tuple[str, int, str], list[tuple[int, dict]]] = {}
    for index, item in enumerate(attempt_events, 1):
        event = item.get("event") if isinstance(item, dict) else None
        event_type = event.get("type") if isinstance(event, dict) else None
        if not isinstance(event_type, str) or event_type not in worker_goal_types:
            continue
        turn_id = item.get("turn_id")
        attempt = event.get("attempt")
        provider = event.get("provider_node_id")
        seq = item.get("seq")
        if (not isinstance(turn_id, str) or not isinstance(attempt, int)
                or isinstance(attempt, bool) or attempt <= 0
                or not isinstance(provider, str) or not provider
                or not isinstance(seq, int) or isinstance(seq, bool) or seq < 0):
            continue
        start_events = starts_by_attempt.get((turn_id, attempt), [])
        matching_starts = [start for start in start_events
                           if start.get("provider_id") == provider]
        if len(start_events) != 1 or len(matching_starts) != 1:
            failures.append(
                f"worker Goal event {index} has no unique matching relay-stamped attempt identity")
            harness = None
        else:
            harness = matching_starts[0].get("agent_kind")
        starts = start_sequences_by_attempt.get((turn_id, attempt), [])
        if len(starts) == 1 and seq <= starts[0]:
            failures.append(
                f"worker Goal event {index} occurs before its relay-stamped attempt start")
        retries = retry_sequences_by_attempt.get((turn_id, attempt), [])
        if len(retries) == 1 and seq >= retries[0]:
            failures.append(
                f"worker Goal event {index} occurs after its relay retry boundary")

        coordinate = (turn_id, attempt, provider)
        if event_type in (
                "goal.slice.completed", "goal.claude.evaluated",
                "goal.claude.evaluator_missing"):
            native_verdicts.setdefault((*coordinate, event_type), []).append((seq, event))
        elif event_type == "goal.claude.set":
            claude_sets.setdefault(coordinate, []).append((seq, event))

        expected_harness = {
            "goal.codex.event": "codex", "goal.slice.completed": "codex",
            "goal.claude.set": "claude", "goal.claude.evaluated": "claude",
            "goal.claude.evaluator_missing": "claude",
        }.get(event_type)
        if expected_harness is not None and harness != expected_harness:
            failures.append(
                f"worker Goal event {index} type {event_type} is only valid for "
                f"{expected_harness} attempts")
        if event_type == "goal.codex.event":
            method = event.get("method")
            if (not isinstance(method, str)
                    or not method.startswith(("turn/", "item/"))):
                failures.append(f"worker Goal event {index} has no valid Codex method")
        elif event_type == "goal.slice.completed":
            if event.get("status") not in (
                    "active", "blocked", "usage_limited", "budget_limited", "complete", "failed"):
                failures.append(f"worker Goal event {index} has no valid Codex Goal status")
            for field in ("turns_completed", "tokens_used"):
                value = event.get(field)
                if (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                    failures.append(
                        f"worker Goal event {index} has no valid {field}")
        elif event_type == "goal.claude.set":
            if event.get("condition") is not None and not isinstance(event.get("condition"), str):
                failures.append(f"worker Goal event {index} has no valid Claude Goal condition")
        elif event_type == "goal.claude.evaluated":
            if not isinstance(event.get("met"), bool):
                failures.append(f"worker Goal event {index} has no boolean Claude met verdict")
            if not isinstance(event.get("impossible"), bool):
                failures.append(
                    f"worker Goal event {index} has no boolean Claude impossible verdict")
            if (event.get("met") is False and event.get("impossible") is False
                    and (not isinstance(event.get("reason"), str) or not event["reason"])):
                failures.append(
                    f"worker Goal event {index} has no reason for an unmet Claude verdict")
            for field in ("reason", "protocol_error"):
                if event.get(field) is not None and not isinstance(event.get(field), str):
                    failures.append(
                        f"worker Goal event {index} has no valid Claude {field}")
        elif event_type == "goal.claude.evaluator_missing":
            if not isinstance(event.get("reason"), str) or not event["reason"]:
                failures.append(
                    f"worker Goal event {index} has no Claude evaluator fallback reason")
            if event.get("fallback") != "independent_grid_eval":
                failures.append(
                    f"worker Goal event {index} has no valid independent Grid eval fallback")

    # A native harness can emit many progress events, but only one terminal verdict per attempt.
    # Duplicate Claude attachments are especially dangerous training data: the final row would not
    # say which verdict actually ended the slice. The optional sentinel is absent from some Claude
    # print-mode streams, but when present it must be unique and precede the evaluator attachment.
    for (turn_id, attempt, provider, event_type), checkpoints in native_verdicts.items():
        if len(checkpoints) > 1:
            failures.append(
                f"turn {turn_id} attempt {attempt} on {provider} has {len(checkpoints)} "
                f"{event_type} verdict checkpoints; expected at most one")
    for coordinate, sentinels in claude_sets.items():
        turn_id, attempt, provider = coordinate
        if len(sentinels) > 1:
            failures.append(
                f"turn {turn_id} attempt {attempt} on {provider} has {len(sentinels)} "
                "Claude Goal set checkpoints; expected at most one")
        verdicts = [
            *native_verdicts.get((*coordinate, "goal.claude.evaluated"), []),
            *native_verdicts.get((*coordinate, "goal.claude.evaluator_missing"), []),
        ]
        if len(sentinels) == 1 and len(verdicts) == 1 and sentinels[0][0] >= verdicts[0][0]:
            failures.append(
                f"turn {turn_id} attempt {attempt} Claude Goal condition was set after its "
                "evaluator verdict")
            for field in ("iterations", "duration_ms", "tokens"):
                value = event.get(field)
                if (value is not None and (not isinstance(value, int)
                                           or isinstance(value, bool) or value < 0)):
                    failures.append(
                        f"worker Goal event {index} has no valid Claude {field}")

    for index, item in enumerate(attempt_events, 1):
        event = item.get("event") if isinstance(item, dict) else None
        event_type = event.get("type") if isinstance(event, dict) else None
        turn_id = item.get("turn_id") if isinstance(item, dict) else None
        seq = item.get("seq") if isinstance(item, dict) else None
        if event_type == "task.claim_expired":
            attempt = event.get("attempt")
            if (isinstance(turn_id, str) and isinstance(attempt, int)
                    and not isinstance(attempt, bool) and attempt > 0
                    and isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0):
                starts = start_sequences_by_attempt.get((turn_id, attempt), [])
                if len(starts) == 1 and seq >= starts[0]:
                    failures.append(
                        f"expired claim event {index} is not before its reused attempt start")
        elif event_type == "task.retrying":
            if not isinstance(event.get("reason"), str) or not event["reason"]:
                failures.append(f"retrying event {index} has no diagnostic reason")
            if (not isinstance(turn_id, str) or not isinstance(seq, int)
                    or isinstance(seq, bool) or seq < 0):
                continue
            enclosing_attempts = []
            for coordinate, starts in start_sequences_by_attempt.items():
                coordinate_turn, attempt = coordinate
                retries = retry_sequences_by_attempt.get(coordinate, [])
                if (coordinate_turn == turn_id and len(starts) == 1 and len(retries) == 1
                        and starts[0] < seq < retries[0]):
                    enclosing_attempts.append(attempt)
            if len(enclosing_attempts) != 1:
                failures.append(
                    f"retrying event {index} is not enclosed by one started, relayed retry "
                    "attempt")
        elif event_type == "task.cancelled":
            failures.append(
                f"completed Goal evidence contains cancellation marker {index}")

    for retry in all_retries:
        event = retry["event"]
        turn_id = retry.get("turn_id")
        attempt = event.get("attempt")
        previous_provider = event.get("previous_provider_id")
        native = [
            item["event"] for item in attempt_events
            if (isinstance(item, dict) and item.get("turn_id") == turn_id
                and isinstance(item.get("event"), dict)
                and isinstance(item["event"].get("type"), str)
                and item["event"].get("type") in native_goal_types
                and item["event"].get("attempt") == attempt
                and item["event"].get("provider_node_id") == previous_provider)
        ]
        for checkpoint in native:
            if checkpoint.get("type") == "goal.slice.completed":
                failures.append(
                    f"turn {turn_id} retried Codex attempt {attempt} also claims a completed "
                    "native Goal slice")
            elif (checkpoint.get("type") == "goal.claude.evaluated"
                  and not checkpoint.get("protocol_error")):
                failures.append(
                    f"turn {turn_id} retried Claude attempt {attempt} has a non-error native "
                    "Goal verdict")
            elif checkpoint.get("type") == "goal.claude.evaluator_missing":
                failures.append(
                    f"turn {turn_id} retried Claude attempt {attempt} also nominated its result "
                    "for independent Grid evaluation")

    # A relay start proves which native harness crossed the execution fence; it does not prove that
    # harness reached its own Goal evaluator. Require the exact native checkpoint from the terminal
    # attempt of every accepted turn. This is the bridge that makes Grid orchestration reuse Codex
    # Goal and Claude /goal rather than silently treating an ordinary agent exit as success.
    last_codex_tokens = 0
    own_tokens = goal.get("own_tokens_used")
    valid_own_tokens = (isinstance(own_tokens, int) and not isinstance(own_tokens, bool)
                        and own_tokens >= 0)
    terminal_attempts = set()
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        identity = (
            turn.get("id"), turn.get("attempt"), turn.get("provider_node_id"),
            turn.get("agent_kind"),
        )
        if (isinstance(identity[0], str)
                and isinstance(identity[1], int) and not isinstance(identity[1], bool)
                and isinstance(identity[2], str) and isinstance(identity[3], str)):
            terminal_attempts.add(identity)
    abandoned_inference_tokens = 0
    abandoned_inference_tokens_complete = True
    for inference_index, item in enumerate(inference, 1):
        if inference_index not in valid_inference_identity or not isinstance(item, dict):
            continue
        identity = (
            item.get("turn_id"), item.get("goal_attempt"),
            item.get("goal_executor_node_id"), item.get("goal_agent_kind"),
        )
        # A native Codex counter advances only when Codex receives a completed model response on
        # the attempt that ultimately completed that turn. Grid intentionally charges every routed
        # request, including work done by a worker that later disappears and a response that times
        # out before Codex can consume it. Those tokens belong in relay `own_tokens_used`, but can
        # never appear in the resumed native thread's counter.
        if identity in terminal_attempts and item.get("state") == "completed":
            continue
        token_fields = (item.get("tokens_in"), item.get("tokens_out"))
        if all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
               for value in token_fields):
            abandoned_inference_tokens += sum(token_fields)
        else:
            abandoned_inference_tokens_complete = False
    for index, turn in enumerate(turns, 1):
        if not isinstance(turn, dict):
            continue
        turn_id = turn.get("id")
        attempt = turn.get("attempt")
        provider = turn.get("provider_node_id")
        harness = turn.get("agent_kind")
        if (not isinstance(turn_id, str) or not isinstance(attempt, int)
                or isinstance(attempt, bool) or attempt <= 0
                or not isinstance(provider, str) or not provider
                or harness not in ("codex", "claude")):
            continue
        native_types = ({"goal.slice.completed"} if harness == "codex" else {
            "goal.claude.evaluated", "goal.claude.evaluator_missing",
        })
        native_items = [
            item for item in attempt_events
            if (isinstance(item, dict) and item.get("turn_id") == turn_id
                and isinstance(item.get("event"), dict)
                and isinstance(item["event"].get("type"), str)
                and item["event"].get("type") in native_types
                and item["event"].get("attempt") == attempt
                and item["event"].get("provider_node_id") == provider)
        ]
        native = [item["event"] for item in native_items]
        if len(native) != 1:
            failures.append(
                f"turn {index} has {len(native)} terminal {harness} Goal checkpoints; expected "
                "exactly one")
            continue
        claude_fallback = (harness == "claude"
                           and native[0].get("type") == "goal.claude.evaluator_missing")
        if claude_fallback:
            # A clean process exit is only a nomination when Claude's private evaluator shape is
            # missing. Accept that provenance bridge solely when the relay independently scored
            # the exact terminal commit after the worker's fallback marker. The accepted run rows
            # are checked against the immutable manifest below; this local check binds their relay
            # marker into the attempt sequence and prevents a bare worker event from substituting
            # for native Goal evidence.
            fallback_seq = native_items[0].get("seq")
            relay_markers = [
                item for item in attempt_events
                if (isinstance(item, dict) and item.get("turn_id") == turn_id
                    and isinstance(item.get("event"), dict)
                    and item["event"].get("type") == "goal.eval.completed"
                    and item["event"].get("passed") is True
                    and item["event"].get("blocked") is False
                    and item["event"].get("result_commit") == turn.get("result_commit")
                    and item["event"].get("checks") == len(evals)
                    and isinstance(item.get("seq"), int)
                    and not isinstance(item.get("seq"), bool)
                    and isinstance(fallback_seq, int)
                    and not isinstance(fallback_seq, bool)
                    and item["seq"] > fallback_seq)
            ]
            if not evals or len(relay_markers) != 1:
                failures.append(
                    f"turn {index} Claude evaluator fallback has no later passing relay "
                    "evaluation marker for its exact result")
        if harness == "codex":
            checkpoint_turns = native[0].get("turns_completed")
            checkpoint_tokens = native[0].get("tokens_used")
            if checkpoint_turns != index:
                failures.append(
                    f"turn {index} Codex Goal checkpoint completed-turn counter does not match "
                    "its accepted trajectory position")
            if (isinstance(checkpoint_tokens, int) and not isinstance(checkpoint_tokens, bool)
                    and checkpoint_tokens >= 0):
                if checkpoint_tokens < last_codex_tokens:
                    failures.append(
                        f"turn {index} Codex Goal token counter moved backward")
                last_codex_tokens = max(last_codex_tokens, checkpoint_tokens)
                if valid_own_tokens and checkpoint_tokens > own_tokens:
                    failures.append(
                        f"turn {index} Codex Goal token counter exceeds relay own-token usage")
                if (index == len(turns) and goal.get("status") == "complete"
                        and valid_own_tokens and checkpoint_tokens != own_tokens
                        and (not abandoned_inference_tokens_complete
                             or checkpoint_tokens + abandoned_inference_tokens != own_tokens)):
                    failures.append(
                        "final Codex Goal token counter plus abandoned inference usage does not "
                        "equal relay own-token usage")
        if index == len(turns) and goal.get("status") == "complete":
            if harness == "codex" and native[0].get("status") != "complete":
                failures.append("final Codex Goal checkpoint is not complete")
            if harness == "claude" and not claude_fallback and (
                    native[0].get("met") is not True
                    or native[0].get("impossible") is not False):
                failures.append("final Claude Goal checkpoint does not prove the condition met")
        if (harness == "claude" and not claude_fallback
                and native[0].get("protocol_error") is not None):
            failures.append(
                f"turn {index} terminal Claude Goal checkpoint contains a protocol error")

    # A killed provider cannot appear as the terminal provider on the reclaimed row. Count it only
    # when the relay-authored retry and attempt-start records agree; either event alone is not
    # sufficient proof that another physical worker executed the Goal.
    execution_nodes.update(verified_retry_nodes)

    if require_worker_revision is not None:
        expected_revision = require_worker_revision.lower()
        required_attempts: set[tuple[str, int, str, str]] = set()
        for turn in turns:
            if not isinstance(turn, dict):
                continue
            coordinate = (
                turn.get("id"), turn.get("attempt"), turn.get("provider_node_id"),
                turn.get("agent_kind"))
            if (isinstance(coordinate[0], str)
                    and isinstance(coordinate[1], int) and not isinstance(coordinate[1], bool)
                    and coordinate[1] > 0
                    and isinstance(coordinate[2], str) and coordinate[2]
                    and coordinate[3] in ("codex", "claude")):
                required_attempts.add(coordinate)
        # A claimed worker can die before the durable attempt-start fence. The relay still records
        # its retry so attempt accounting is complete, but no native harness executed and there is
        # no runtime to attest. Require provenance only for retry predecessors whose relay-stamped
        # start agreed with the retry—the same exact set allowed to count as an execution node.
        required_attempts.update(verified_retry_attempts)

        for turn_id, attempt, provider, harness in sorted(required_attempts):
            starts = [
                start for start in starts_by_attempt.get((turn_id, attempt), [])
                if start.get("provider_id") == provider
                and start.get("agent_kind") == harness
            ]
            if len(starts) != 1:
                failures.append(
                    f"turn {turn_id} attempt {attempt} has {len(starts)} relay-stamped start "
                    "records for required worker provenance; expected exactly one")
                continue
            runtime = starts[0].get("worker_runtime")
            grid_runtime = runtime.get("grid") if isinstance(runtime, dict) else None
            agent_runtime = runtime.get("agent") if isinstance(runtime, dict) else None
            revision = grid_runtime.get("revision") if isinstance(grid_runtime, dict) else None
            if (not isinstance(runtime, dict) or runtime.get("schema_version") != 1
                    or not isinstance(grid_runtime, dict)
                    or not isinstance(grid_runtime.get("version"), str)
                    or not isinstance(revision, str)
                    or re.fullmatch(r"[0-9a-f]{7,64}", revision) is None
                    or grid_runtime.get("dirty") is not False
                    or not isinstance(agent_runtime, dict)
                    or agent_runtime.get("kind") != harness
                    or not isinstance(agent_runtime.get("version"), str)
                    or not agent_runtime.get("version")):
                failures.append(
                    f"turn {turn_id} attempt {attempt} has no valid clean relay-stamped worker "
                    "runtime and native agent version")
            elif not revision.startswith(expected_revision):
                failures.append(
                    f"turn {turn_id} attempt {attempt} used Grid worker revision {revision}, not "
                    f"required revision {expected_revision}")

    if len(execution_nodes) < min_execution_nodes:
        failures.append(
            f"Goal used {len(execution_nodes)} distinct execution node(s), fewer than required "
            f"{min_execution_nodes}")
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
    native_retry_turn_ids = {
        retry.get("turn_id") for retry in native_retries
        if isinstance(retry.get("turn_id"), str)}
    for turn_id in native_retry_turn_ids:
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
        # These evaluators are executed by deterministic relay code, never by the acting harness.
        # A random nonempty node name is not proof of independent evaluation in a release artifact.
        expected_evaluator = (
            "relay" if spec.get("type") in ("file", "json", "verify") else None)
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
        elif spec.get("type") == "verify":
            for run in accepted:
                failures.extend(_verify_eval_events(spec, run, attempt_events, final_turn))

    # The checks above prove that every final metric has a passing witness. Training safety needs
    # the converse too: every row labelled authoritative must be one of this Goal's immutable
    # metrics, internally consistent, relay-authored, and tied to the exact completed turn commit.
    # Otherwise an extra poisoned label can ride beside a perfectly valid final witness and pass
    # verification simply because nothing asked where that extra accepted row came from.
    manifest = {
        (spec.get("definition_id"), spec.get("definition_hash")): spec
        for spec in evals
        if (isinstance(spec, dict)
            and isinstance(spec.get("definition_id"), str) and spec.get("definition_id")
            and isinstance(spec.get("definition_hash"), str) and spec.get("definition_hash"))
    }
    accepted_coordinates: set[tuple[str, str, tuple[str, str]]] = set()
    accepted_ids: set[str] = set()
    for index, run in enumerate(runs, 1):
        if not isinstance(run, dict):
            failures.append(f"evaluation run {index} is not an object")
            continue
        if run.get("accepted") is not True:
            if run.get("accepted_at") is not None:
                failures.append(
                    f"rejected evaluation run {run.get('id') or index} has an acceptance timestamp")
            continue
        run_id = run.get("id")
        if not isinstance(run_id, str) or not run_id:
            failures.append(f"accepted evaluation run {index} has no immutable run id")
        elif run_id in accepted_ids:
            failures.append(f"accepted evaluation run id {run_id} appears more than once")
        else:
            accepted_ids.add(run_id)
        definition_id = run.get("definition_id")
        definition_hash = run.get("definition_hash")
        identity = ((definition_id, definition_hash)
                    if isinstance(definition_id, str) and isinstance(definition_hash, str)
                    else None)
        spec = manifest.get(identity) if identity is not None else None
        if spec is None:
            failures.append(
                f"accepted evaluation run {run_id or index} does not match the immutable "
                "Goal eval manifest")
        elif (spec.get("type") in ("file", "json", "verify")
              and run.get("evaluator_node_id") != "relay"):
            failures.append(
                f"accepted evaluation run {run_id or index} was not authored by the relay")
        run_turn_id = run.get("turn_id")
        turn = (turns_by_id.get(run_turn_id)
                if isinstance(run_turn_id, str) else None)
        if turn is None:
            failures.append(
                f"accepted evaluation run {run_id or index} names an unknown Goal turn")
        elif run.get("result_commit") != turn.get("result_commit"):
            failures.append(
                f"accepted evaluation run {run_id or index} does not score its turn's exact "
                "result commit")
        state = run.get("state")
        passed = run.get("passed")
        score = run.get("score")
        if (state not in ("passed", "failed") or not isinstance(passed, bool)
                or passed != (state == "passed")
                or isinstance(score, bool) or not isinstance(score, (int, float))
                or score != (1.0 if passed else 0.0) or run.get("error") is not None):
            failures.append(
                f"accepted evaluation run {run_id or index} has an inconsistent verdict")
        accepted_at = run.get("accepted_at")
        if not isinstance(accepted_at, str) or not accepted_at:
            failures.append(
                f"accepted evaluation run {run_id or index} has no acceptance timestamp")
        result_commit = run.get("result_commit")
        if isinstance(run_turn_id, str) and isinstance(result_commit, str) and identity is not None:
            coordinate = (run_turn_id, result_commit, identity)
            if coordinate in accepted_coordinates:
                failures.append(
                    f"accepted evaluation run {run_id or index} duplicates a "
                    "turn/commit/metric verdict")
            else:
                accepted_coordinates.add(coordinate)

    # `goal.eval.completed` is appended immediately before `task.terminal` in the same guarded
    # transaction that accepts the exact evaluation rows. Cross-check all three representations so
    # a valid score row cannot hide a missing, stale, duplicated, or contradictory relay verdict.
    eval_markers: dict[str, list[tuple[int, int, dict]]] = {}
    for index, item in enumerate(attempt_events, 1):
        if (not isinstance(item, dict) or not isinstance(item.get("event"), dict)
                or item["event"].get("type") != "goal.eval.completed"):
            continue
        event = item["event"]
        turn_id = item.get("turn_id")
        seq = item.get("seq")
        if not isinstance(turn_id, str) or turn_id not in turn_ids:
            failures.append(f"evaluation marker {index} names an unknown Goal turn")
            continue
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            failures.append(f"evaluation marker {index} has no valid relay sequence")
            continue
        if not isinstance(event.get("passed"), bool):
            failures.append(f"evaluation marker {index} has no boolean passed verdict")
        if not isinstance(event.get("blocked"), bool):
            failures.append(f"evaluation marker {index} has no boolean blocked verdict")
        if event.get("passed") is True and event.get("blocked") is True:
            failures.append(f"evaluation marker {index} is both passed and blocked")
        turn = turns_by_id.get(turn_id) or {}
        if event.get("result_commit") != turn.get("result_commit"):
            failures.append(
                f"evaluation marker {index} does not score its turn's exact result commit")
        checks = event.get("checks")
        if (not isinstance(checks, int) or isinstance(checks, bool)
                or checks != len(evals)):
            failures.append(
                f"evaluation marker {index} check count does not match the immutable Goal eval "
                "manifest")
        terminals = terminal_events.get(turn_id, [])
        if len(terminals) == 1 and seq >= terminals[0][1]:
            failures.append(
                f"evaluation marker {index} is not before its relay terminal event")
        eval_markers.setdefault(turn_id, []).append((index, seq, event))

    for turn_id, markers in eval_markers.items():
        if len(markers) != 1:
            failures.append(
                f"turn {turn_id} has {len(markers)} relay evaluation markers; expected at most "
                "one")
            continue
        marker_index, _seq, marker = markers[0]
        commit = marker.get("result_commit")
        matching_runs = [
            run for run in runs
            if (isinstance(run, dict) and run.get("turn_id") == turn_id
                and run.get("result_commit") == commit
                and (run.get("definition_id"), run.get("definition_hash")) in manifest)
        ]
        accepted_by_identity = {
            identity: [run for run in matching_runs
                       if run.get("accepted") is True
                       and (run.get("definition_id"), run.get("definition_hash")) == identity]
            for identity in manifest
        }
        if marker.get("passed") is True:
            if any(len(rows) != 1 or rows[0].get("state") != "passed"
                   or rows[0].get("passed") is not True
                   for rows in accepted_by_identity.values()):
                failures.append(
                    f"evaluation marker {marker_index} passed verdict is not proven by exactly "
                    "one accepted passing row for every immutable metric")
        elif marker.get("blocked") is True:
            if not any(run.get("state") == "error" and run.get("accepted") is not True
                       for run in matching_runs):
                failures.append(
                    f"evaluation marker {marker_index} blocked verdict has no matching rejected "
                    "evaluator error")
        elif (len(accepted_by_identity) != len(manifest)
              or any(len(rows) != 1 for rows in accepted_by_identity.values())
              or not any(rows[0].get("state") == "failed"
                         for rows in accepted_by_identity.values() if rows)):
            failures.append(
                f"evaluation marker {marker_index} failed verdict is not proven by one accepted "
                "row per immutable metric and at least one failure")

    for index, run in enumerate(runs, 1):
        if not isinstance(run, dict) or run.get("accepted") is not True:
            continue
        run_turn_id = run.get("turn_id")
        matching_markers = [
            marker for _marker_index, _seq, marker in (
                eval_markers.get(run_turn_id, []) if isinstance(run_turn_id, str) else [])
            if marker.get("result_commit") == run.get("result_commit")]
        if len(matching_markers) != 1:
            failures.append(
                f"accepted evaluation run {run.get('id') or index} has "
                f"{len(matching_markers)} matching relay evaluation markers; expected exactly "
                "one")

    final_markers = (eval_markers.get(final_turn_id, [])
                     if isinstance(final_turn_id, str) else [])
    if evals:
        if len(final_markers) != 1:
            failures.append(
                f"final Goal turn has {len(final_markers)} relay evaluation markers; expected "
                "exactly one")
        elif (final_markers[0][2].get("passed") is not True
              or final_markers[0][2].get("blocked") is not False):
            failures.append("final Goal evaluation marker is not an unblocked passing verdict")
    elif eval_markers:
        failures.append("Goal without evals contains relay evaluation markers")
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
            allow_subgoals=getattr(args, "allow_subgoals", False),
            idempotency_key=(getattr(args, "idempotency_key", None)
                             or str(uuid.uuid4())))
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
                waiting = (isinstance(goal.get("model_readiness"), dict)
                           and goal["model_readiness"].get("state") == "waiting")
                status = "waiting-model" if waiting else goal.get("status")
                print(f"  {status!s:<15} {goal.get('id')}  {goal.get('objective')}")
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
                require_inference=getattr(args, "require_inference", False),
                require_worker_revision=getattr(args, "require_worker_revision", None),
                require_agent_sequence=getattr(args, "require_agent_sequence", None))
            if failures:
                raise SystemExit("Goal evidence verification failed:\n- " + "\n- ".join(failures))
            print("Goal evidence verified: terminal turns, transcript chain and evaluations pass.",
                  file=sys.stderr)
        return 0
    else:
        goal = relay.control_goal(
            base, token, args.goal_id, args.goal_action,
            token_budget=(getattr(args, "token_budget", None)
                          if args.goal_action == "resume" else None))
    _show(goal, args.json)
    return 0
