from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from train import goal_dataset


def _evidence(goal_id: str, *, text: str = "done") -> dict:
    return {
        "schema_version": 1,
        "goal": {"id": goal_id, "status": "complete", "objective": "build it",
                 "done_when": "tests pass", "model": "local-model",
                 "evals": [{"definition_id": "eval-1", "kind": "command"}]},
        "turns": [{"id": f"turn-{goal_id}", "prompt": "continue",
                   "output": text, "state": "completed"}],
        "attempt_events": [{"turn_id": f"turn-{goal_id}", "event": {
            "type": "goal.act.request", "tool": "crm.update",
            "arguments": {"authorization": "Bearer should-not-leak",
                          "email": "person@example.com"}}}],
        "inference": [{"turn_id": f"turn-{goal_id}", "model": "local-model"}],
        "eval_runs": [{"id": f"run-{goal_id}", "turn_id": f"turn-{goal_id}",
                       "definition_id": "eval-1", "state": "passed", "passed": True,
                       "accepted": True, "score": 0.9,
                       "evidence": {"access_token": "top-secret"}}],
        "relationships": {"parent_goal_id": None, "children": []},
    }


def test_dataset_verifies_redacts_deduplicates_and_splits_whole_goals(tmp_path):
    evidence = {"g1": _evidence("g1", text="contact me at person@example.com"),
                "g2": _evidence("g2", text="a distinct answer"),
                "g3": _evidence("g3", text="a distinct answer")}
    # Turn/worker ids do not enter the fingerprint. This is deliberate: rerunning identical work
    # on another machine is still one teaching example.
    result = goal_dataset.build_dataset(
        [{"id": key, "status": "complete"} for key in evidence], evidence.__getitem__,
        tmp_path / "dataset", grid="forge", holdout_fraction=0.5,
        verify=lambda _record: [])
    assert (result.accepted, result.duplicates, result.train, result.held_out) == (2, 1, 1, 1)
    rows = []
    for split in ("train", "held_out"):
        rows += [json.loads(line) for line in
                 (tmp_path / "dataset" / split / "trajectories.jsonl").read_text().splitlines()]
    dumped = json.dumps(rows)
    assert "person@example.com" not in dumped
    assert "should-not-leak" not in dumped and "top-secret" not in dumped
    assert "[email]" in dumped and "[secret]" in dumped
    assert {row["split"] for row in rows} == {"train", "held_out"}
    manifest = json.loads((tmp_path / "dataset" / "manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["counts"] == {"accepted": 2, "duplicates": 1, "held_out": 1,
                                  "rejected": 1, "train": 1}
    assert manifest["files"]["train/sft.jsonl"]


def test_dataset_refuses_self_graded_incomplete_and_failed_evidence(tmp_path):
    no_eval = _evidence("no-eval")
    no_eval["goal"]["evals"] = []
    bad_eval = _evidence("bad-eval")
    bad_eval["eval_runs"][0]["passed"] = False
    result = goal_dataset.build_dataset(
        [{"id": "active", "status": "active"},
         {"id": "no-eval", "status": "complete"},
         {"id": "bad-eval", "status": "complete"}],
        {"no-eval": no_eval, "bad-eval": bad_eval}.__getitem__, tmp_path / "dataset",
        grid="forge", verify=lambda record: (["tampered evidence"]
                                              if record is bad_eval else []))
    assert result.accepted == 0 and result.rejected == 3
    rejected = [json.loads(line) for line in
                (tmp_path / "dataset" / "rejected.jsonl").read_text().splitlines()]
    assert any("no independent Grid eval" in " ".join(row["reasons"]) for row in rejected)
    assert any("tampered evidence" in " ".join(row["reasons"]) for row in rejected)


def test_dataset_refuses_to_overwrite_without_force(tmp_path):
    destination = tmp_path / "dataset"
    destination.mkdir()
    (destination / "mine.txt").write_text("keep")
    with pytest.raises(FileExistsError):
        goal_dataset.build_dataset([], lambda _goal_id: {}, destination, grid="forge")
    assert (destination / "mine.txt").read_text() == "keep"


def test_dataset_cli_reads_selected_remote_grid(monkeypatch, tmp_path, capsys):
    from cli.train import cmd_train_dataset
    from remote import relay

    monkeypatch.setattr("cli.remote_task._resolve", lambda _args: ("http://relay", "token", "forge"))
    monkeypatch.setattr(relay, "list_goals", lambda *_args, **_kwargs:
                        [{"id": "g1", "status": "complete"}])
    monkeypatch.setattr(relay, "get_goal_evidence", lambda *_args: _evidence("g1"))
    monkeypatch.setattr("cli.goal._verify_evidence", lambda *_args, **_kwargs: [])
    args = SimpleNamespace(out=str(tmp_path / "dataset"), goal_id=[], holdout=0.1,
                           seed="1729", format="jsonl", force=False, grid="forge")
    assert cmd_train_dataset(args) == 0
    assert "1 verified Goal trajectories" in capsys.readouterr().out


def test_parser_exposes_goal_dataset_surface():
    from cli.parser import build_parser

    args = build_parser().parse_args(
        ["train", "dataset", "--grid", "forge", "--format", "both", "--holdout", "0.2"])
    assert args.grid == "forge" and args.format == "both" and args.holdout == 0.2
