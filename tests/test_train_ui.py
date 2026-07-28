"""Dashboard tests: run discovery from GRID_HOME artifacts + page/API rendering."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from train.ui import build_app, load_runs


def _fixture_run(tmp_path, name="torch-hello-20260101-000000"):
    run_dir = tmp_path / "artifacts" / "train" / name
    run_dir.mkdir(parents=True)
    points = [
        {"step": 1, "reward_mean": 0.1, "loss": -0.01},
        {"step": 5, "reward_mean": 0.3, "eval": 0.4},
        {"step": 10, "reward_mean": 0.6, "eval": 0.7},
    ]
    (run_dir / "log.jsonl").write_text(
        "\n".join(json.dumps(p) for p in points) + "\n", encoding="utf-8"
    )
    (run_dir / "summary.json").write_text(
        json.dumps({"backend": "torch/cpu", "model": "m", "baseline_eval": 0.2,
                    "final_eval": 0.7, "minutes": 5.6}),
        encoding="utf-8",
    )
    return run_dir


def test_load_runs_reads_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _fixture_run(tmp_path)
    runs = load_runs()
    assert len(runs) == 1
    assert runs[0]["summary"]["final_eval"] == 0.7
    assert len(runs[0]["points"]) == 3


def test_load_runs_tolerates_running_and_garbage(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    run_dir = _fixture_run(tmp_path)
    (run_dir / "summary.json").unlink()  # in-progress run: log but no summary yet
    with (run_dir / "log.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("not json\n")  # partial write mid-run must not kill the page
    runs = load_runs()
    assert runs[0]["summary"] == {}
    assert len(runs[0]["points"]) == 3


def test_page_and_api(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    _fixture_run(tmp_path)
    client = TestClient(build_app())
    api = client.get("/api/runs")
    assert api.status_code == 200 and api.json()[0]["name"].startswith("torch-hello")
    page = client.get("/")
    assert page.status_code == 200
    body = page.text
    assert "torch-hello-20260101-000000" in body
    assert "<svg" in body and "polyline" in body  # the curve rendered
    assert "table view" in body  # accessibility: table view exists
    assert "running" not in body  # summary present -> not marked live


def test_page_empty_state(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    client = TestClient(build_app())
    page = client.get("/")
    assert "No training runs yet" in page.text
