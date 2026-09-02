from __future__ import annotations

import json

import pytest

from train.goal_benchmark import load_suite, run_benchmark


def _suite():
    return {"version": 1, "project": "sandbox", "cases": [{
        "id": "build-game", "objective": "Build a game", "done_when": "all checks pass",
        "evals": [{"kind": "artifact", "path": "game.html"}],
    }]}


def test_suite_requires_measurable_independent_cases(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(_suite()))
    assert load_suite(path)["cases"][0]["id"] == "build-game"
    broken = _suite()
    broken["cases"][0]["evals"] = []
    path.write_text(json.dumps(broken))
    with pytest.raises(ValueError, match="independent Grid eval"):
        load_suite(path)


def test_benchmark_submits_all_cases_then_reports_simple_pass_rate(tmp_path):
    suite = _suite()
    suite["cases"].append({"id": "bad", "objective": "Do bad", "done_when": "check",
                           "evals": [{"kind": "artifact", "path": "x"}]})
    submitted = []
    statuses = {"goal-build-game": "complete", "goal-bad": "failed"}

    result = run_benchmark(
        suite, tmp_path / "run", grid="forge", model="local", threshold=0.5,
        poll_seconds=0, submit=lambda case, _project, _key: (
            submitted.append(case["id"]) or {"id": f"goal-{case['id']}", "status": "active"}),
        get_status=lambda goal_id: {"status": statuses[goal_id]},
        get_evidence=lambda goal_id: {"goal": {"id": goal_id, "access_token": "hidden"}},
        resolve_project=lambda project: f"project-{project}", verify=lambda _evidence: [])
    assert submitted == ["build-game", "bad"]
    assert (result.passed, result.total, result.pass_rate, result.met_threshold) == (1, 2, 0.5, True)
    state = json.loads((tmp_path / "run" / "benchmark.json").read_text())
    assert state["result"]["pass_rate"] == 0.5
    evidence = (tmp_path / "run" / "evidence" / "build-game-1.json").read_text()
    assert "hidden" not in evidence and "[secret]" in evidence


def test_benchmark_resume_reuses_goal_ids_and_idempotency_keys(tmp_path):
    submitted = []
    common = dict(
        suite=_suite(), run_dir=tmp_path / "run", grid="forge", model="local",
        submit=lambda case, _project, key: (
            submitted.append((case["id"], key)) or {"id": "goal-1", "status": "active"}),
        get_status=lambda _goal_id: {"status": "active"}, get_evidence=lambda _goal_id: {},
        resolve_project=lambda project: project, verify=lambda _evidence: [])
    first = run_benchmark(**common, wait=False)
    second = run_benchmark(**common, wait=False)
    assert not first.complete and not second.complete
    assert len(submitted) == 1


def test_benchmark_persists_idempotency_key_before_uncertain_create(tmp_path):
    def uncertain(*_args):
        raise SystemExit("response lost")

    common = dict(
        suite=_suite(), run_dir=tmp_path / "run", grid="forge", model="local", wait=False,
        submit=uncertain, get_status=lambda _goal_id: {}, get_evidence=lambda _goal_id: {},
        resolve_project=lambda project: project, verify=lambda _evidence: [])
    with pytest.raises(SystemExit, match="response lost"):
        run_benchmark(**common)
    first_key = json.loads((tmp_path / "run" / "benchmark.json").read_text())["cases"][0][
        "idempotency_key"]
    observed = []
    common["submit"] = lambda _case, _project, key: (
        observed.append(key) or {"id": "goal-1", "status": "active"})
    run_benchmark(**common)
    assert observed == [first_key]


def test_benchmark_requires_same_inputs_when_resuming(tmp_path):
    common = dict(
        suite=_suite(), run_dir=tmp_path / "run", grid="forge", model="local", wait=False,
        submit=lambda *_args: {"id": "goal-1", "status": "active"},
        get_status=lambda _goal_id: {}, get_evidence=lambda _goal_id: {},
        resolve_project=lambda project: project, verify=lambda _evidence: [])
    run_benchmark(**common)
    with pytest.raises(ValueError, match="different benchmark"):
        run_benchmark(**{**common, "model": "other"})


def test_benchmark_timeout_is_resumable_not_a_terminal_failure(tmp_path, monkeypatch):
    ticks = iter((0.0, 2.0))
    monkeypatch.setattr("train.goal_benchmark.time.monotonic", lambda: next(ticks))
    result = run_benchmark(
        _suite(), tmp_path / "run", grid="forge", model="local", timeout_seconds=1,
        poll_seconds=0, submit=lambda *_args: {"id": "goal-1", "status": "active"},
        get_status=lambda _goal_id: {"status": "active"}, get_evidence=lambda _goal_id: {},
        resolve_project=lambda project: project, verify=lambda _evidence: [])
    assert not result.complete and result.pass_rate == 0
    state = json.loads((tmp_path / "run" / "benchmark.json").read_text())
    assert "rerun" in state["cases"][0]["failures"][0]


def test_parser_exposes_goal_benchmark_surface():
    from cli.parser import build_parser

    args = build_parser().parse_args([
        "train", "benchmark", "--suite", "suite.json", "--model", "qwen",
        "--run-dir", "run", "--grid", "forge", "--repeat", "20",
        "--min-execution-nodes", "3", "--require-agent-sequence", "codex,claude"])
    assert args.repeat == 20 and args.min_execution_nodes == 3
    assert args.require_agent_sequence == ("codex", "claude")
