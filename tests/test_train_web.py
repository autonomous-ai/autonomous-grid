"""Web-interface tests: the whole wizard a support manager walks through, and every refusal.

The refusals matter more than the happy path — this interface exists to stop someone spending a
night of their office's compute on data that can't teach anything, or serving a model that didn't
earn it.
"""
from __future__ import annotations

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from train.web import build_app, prepare, workspace


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    return TestClient(build_app(), follow_redirects=False)


def _tickets_csv(n: int = 60) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Subject", "Description", "Public reply", "Status"])
    for i in range(n):
        writer.writerow([
            f"Desk {i} stuck at lowest height",
            f"My SmartDesk (order AN-{100000 + i}) stopped moving and shows E07. Please help.",
            ("Sorry about the E07 — that is the anti-collision sensor tripping. Unplug the desk "
             "for sixty seconds, then hold the down button for ten seconds until it recalibrates. "
             f"If it returns I will ship a replacement controller for AN-{100000 + i}."),
            "solved",
        ])
    return buf.getvalue()


def _leads_jsonl(per_class: int = 12) -> str:
    rows = []
    for i in range(per_class):
        rows.append({"lead": f"We are a {20 + i}-person firm, budget approved, need 30 desks",
                     "outcome": "Closed Won"})
        rows.append({"lead": f"Do you ship the pod to Canada? Question {i}",
                     "outcome": "Qualified"})
        rows.append({"lead": f"CONGRATULATIONS you won our SEO program {i}", "outcome": "Spam"})
    return "".join(json.dumps(r) + "\n" for r in rows)


# --- the data report: the honest gate before anyone waits a night --------------------------

def test_support_report_accepts_a_real_looking_zendesk_export():
    report, kept = prepare.prepare("support-replies", _tickets_csv(300), "export.csv")
    assert report.ok and report.level == "good"
    assert report.rows_usable == 300
    assert report.columns_used["reply"] == "public reply"   # column guessed, not demanded
    assert len(report.samples) == 3
    assert len(kept) == 300


def test_support_report_refuses_too_little_and_says_what_to_do():
    report, _ = prepare.prepare("support-replies", _tickets_csv(12), "export.csv")
    assert not report.ok and report.level == "blocked"
    assert "12 usable tickets" in report.headline
    assert "longer date range" in report.detail


def test_support_report_names_the_missing_column():
    csv_text = "subject,description\nBroken desk,It does not move\n"
    report, _ = prepare.prepare("support-replies", csv_text, "x.csv")
    assert not report.ok
    assert "missing the reply column" in report.headline
    assert "description" in report.detail  # shows what it DID find


def test_support_report_survives_a_junk_file():
    report, _ = prepare.prepare("support-replies", "this is not a spreadsheet at all", "notes.txt")
    assert not report.ok
    assert "couldn't read" in report.headline or "missing" in report.headline


def test_leads_report_balances_classes_and_maps_crm_stage_names():
    report, kept = prepare.prepare("sales-triage", _leads_jsonl(40), "leads.jsonl")
    assert report.ok
    # "Closed Won" → hot, "Qualified" → warm, "Spam" → cold; balanced to the smallest class.
    assert set(report.distribution) == {"hot", "warm", "cold"}
    assert len({row["label"] for row in kept}) == 3
    assert report.rows_usable == 3 * min(report.distribution.values())


def test_leads_report_refuses_when_a_class_is_tiny():
    rows = [{"lead": "big deal", "outcome": "Closed Won"}]
    rows += [{"lead": f"junk {i}", "outcome": "Spam"} for i in range(50)]
    report, _ = prepare.prepare("sales-triage", "".join(json.dumps(r) + "\n" for r in rows), "l.jsonl")
    assert not report.ok
    assert "smallest group" in report.detail


# --- the wizard, end to end ---------------------------------------------------------------

def test_full_wizard_to_the_point_of_training(client, tmp_path):
    assert client.get("/").status_code == 200
    assert "Teach a new model" in client.get("/").text

    created = client.post("/new", data={"pack": "support-replies", "name": "Support replies"})
    assert created.status_code == 303
    slug = created.headers["location"].rsplit("/", 1)[-1]

    page = client.get(f"/w/{slug}")
    assert "Add your examples" in page.text

    upload = client.post(f"/w/{slug}/data",
                         json={"filename": "export.csv", "content": _tickets_csv(300)})
    assert upload.status_code == 200 and upload.json()["ok"] is True

    # The report stays on screen — it is the point of this step, not a splash on the way past.
    page = client.get(f"/w/{slug}")
    assert "usable tickets" in page.text
    assert "What it will learn from" in page.text
    assert "Next — what good looks like" in page.text

    page = client.get(f"/w/{slug}/checks")
    assert "What does a good answer look like" in page.text
    assert "Sounds like the replies your team actually sent" in page.text

    picked = client.post(f"/w/{slug}/checks", data={"check": ["similarity", "format"]})
    assert picked.status_code == 303
    rewards = (workspace.workspaces_root() / slug / "rewards.py").read_text(encoding="utf-8")
    assert "def reward_similarity" in rewards
    assert "def reward_format" in rewards
    assert "def reward_grounding" not in rewards      # unticked → not generated
    assert "def reward_judge" not in rewards          # off by default: it costs time and a judge

    # The machines step asks nothing an engineer would have to answer. The collapsed "Somewhere
    # else" block is the deliberate escape hatch and may speak in addresses; the main flow may not.
    page = client.get(f"/w/{slug}").text
    main_flow = page.split("Somewhere else")[0]
    assert "Where should it learn?" in page
    assert "How long to give it" in page
    for engineer_words in ("/v1", "attempts)", "Qwen/Qwen3", "endpoint", "GRPO", "LoRA", "adapter"):
        assert engineer_words not in main_flow, f"{engineer_words!r} leaked into the main flow"
    # And what she does see is a choice with a cost attached.
    assert "Recommended" in page and "GB" in page

    # Starting writes a config the CLI could run by hand — then launches it.
    config_text = workspace.write_config(
        workspace.load(slug), model="Qwen/Qwen3-4B-Instruct-2507",
        endpoint="http://127.0.0.1:8080/v1", steps=120,
    ).read_text(encoding="utf-8")
    assert 'sync_every = 2' in config_text
    assert "prompts.jsonl" in config_text and "rewards.py" in config_text


def test_generated_config_is_loadable_by_the_cli(client, tmp_path):
    """The web interface must not invent a private format — the CLI has to accept its output."""
    from train.config import load_config

    created = client.post("/new", data={"pack": "sales-triage", "name": "Lead triage"})
    slug = created.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/w/{slug}/data", json={"filename": "leads.jsonl", "content": _leads_jsonl(40)})
    client.get(f"/w/{slug}/checks")
    client.post(f"/w/{slug}/checks", data={"check": ["parseable"]})
    w = workspace.load(slug)
    path = workspace.write_config(w, model="Qwen/Qwen3-4B-Instruct-2507",
                                 endpoint="http://127.0.0.1:8080/v1", steps=60)
    cfg = load_config(path)
    assert cfg.rollout.sync_every == 2
    assert cfg.trainer.steps == 60
    assert cfg.deploy.adapter_name == slug
    # The locked check is always generated even if the box wasn't submitted.
    rewards = (w.path / "rewards.py").read_text(encoding="utf-8")
    assert "def reward_correct_priority" in rewards


def test_checks_page_refuses_before_there_is_data(client):
    created = client.post("/new", data={"pack": "support-replies", "name": "x"})
    slug = created.headers["location"].rsplit("/", 1)[-1]
    response = client.get(f"/w/{slug}/checks")
    assert response.status_code == 400
    assert "Add some examples first" in response.text


def test_upload_rejects_empty_and_oversized(client):
    created = client.post("/new", data={"pack": "support-replies", "name": "x"})
    slug = created.headers["location"].rsplit("/", 1)[-1]
    assert client.post(f"/w/{slug}/data", json={"filename": "a.csv", "content": "   "}).status_code == 400


def test_unknown_model_is_a_404_not_a_crash(client):
    assert client.get("/w/nope").status_code == 404


def test_cannot_serve_a_model_that_was_never_checked(client):
    created = client.post("/new", data={"pack": "support-replies", "name": "x"})
    slug = created.headers["location"].rsplit("/", 1)[-1]
    response = client.post(f"/w/{slug}/use")
    assert response.status_code == 400
    assert "Run the comparison first" in response.text


def test_cannot_serve_a_model_that_lost(client):
    created = client.post("/new", data={"pack": "support-replies", "name": "x"})
    slug = created.headers["location"].rsplit("/", 1)[-1]
    w = workspace.load(slug)
    run = w.path / "run"
    run.mkdir(parents=True)
    (run / "eval-card.json").write_text(json.dumps({
        "passed": False, "delta": -0.1, "verdict": "no meaningful gain",
        "before": {"per_grader": {}, "overall": 0.5, "samples": [], "n": 4},
        "after": {"per_grader": {}, "overall": 0.4, "samples": [], "n": 4},
    }), encoding="utf-8")
    response = client.post(f"/w/{slug}/use")
    assert response.status_code == 400
    assert "did not earn it" in response.text


def test_machines_page_is_honest_when_nothing_can_train(client, monkeypatch):
    monkeypatch.setattr("train.web.routes.detect_engines", lambda **kw: [], raising=False)
    page = client.get("/machines")
    assert page.status_code == 200
    assert "machines helping" in page.text


def test_api_workspaces_lists_state(client):
    client.post("/new", data={"pack": "support-replies", "name": "Support replies"})
    rows = client.get("/api/workspaces").json()
    assert rows and rows[0]["pack"] == "support-replies"
    assert rows[0]["stage"] == "data"


def test_curve_plots_whichever_signal_the_run_produces():
    """A feedback run scores attempts; an imitation run reports loss. The Mac path is the second
    one, and it used to render "no scores yet" for an entire night."""
    from train.web.pages import curve_svg

    empty = curve_svg([])
    assert "Nothing to plot yet" in empty

    rising = curve_svg([{"step": i, "reward_mean": i / 20} for i in range(1, 21)])
    assert rising.count("<polyline") == 2      # raw scores plus the smoothed trend
    assert "higher is better" in rising
    assert "aria-label" in rising

    falling = curve_svg([{"step": i * 5, "loss": 4.0 - i * 0.1} for i in range(1, 15)])
    assert falling.count("<polyline") == 2
    assert "lower is better" in falling


# --- honest run state: a dead trainer must never read as "learning" ------------------------

def test_a_failed_run_says_so_and_offers_a_way_forward(tmp_path, monkeypatch):
    """The page that said "learning now" for eleven hours after a crash was worse than an error."""
    import json as _json
    import time

    from train.web import jobs, pages, workspace

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("Support replies", "support-replies")
    (w.path / "run").mkdir(parents=True, exist_ok=True)
    (w.path / "run.log").write_text(
        "Loading model\ngrid train: only 8 usable tickets — export a longer date range.\n",
        encoding="utf-8",
    )
    (w.path / "job.json").write_text(_json.dumps({
        "pid": 999999, "started": time.time() - 90, "config": "x", "verb": "sft",
        "expected_steps": 600, "exit": 1, "ended": time.time(),
    }), encoding="utf-8")

    job = jobs.status(w.path)
    assert job["state"] == "failed"
    assert job["running"] is False
    # It quotes the trainer's own plain sentence rather than inventing one from an exit code
    # (sentence-cased for display).
    assert "8 usable tickets" in job["reason"]
    assert job["reason"].startswith("Only")

    page = pages.running_step(w, job)
    assert "stopped" in page
    assert "8 usable tickets" in page
    assert "Start again" in page and "Change what good looks like" in page
    assert "learning now" not in page
    assert 'http-equiv="refresh"' not in page      # a stopped run must not keep polling


def test_a_run_that_vanished_is_reported_as_stopped(tmp_path, monkeypatch):
    import json as _json
    import time

    from train.web import jobs, workspace

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("x", "support-replies")
    (w.path / "job.json").write_text(_json.dumps({
        "pid": 999999, "started": time.time() - 600, "config": "x", "verb": "run",
    }), encoding="utf-8")
    job = jobs.status(w.path)
    assert job["state"] == "stopped"
    assert "asleep" in job["reason"] or "ended" in job["reason"]


def test_progress_page_estimates_time_and_names_the_phase():
    from train.web.jobs import describe_phase, estimate

    assert estimate(0, 600, 5) == ""                      # too early to guess
    assert "minutes left" in estimate(60, 600, 300)
    assert "hours left" in estimate(10, 600, 600)
    assert estimate(600, 600, 100) == ""                  # finished: nothing to estimate
    assert "starting model" in describe_phase("Downloading shards: 2.1G/8.0GB")
    assert "up to date" in describe_phase("[grid train] step 8: 4/4 nodes synced")
    assert "your examples" in describe_phase("Iter 30: Train loss 2.1")
    assert describe_phase("") == "Getting started."


def test_nothing_is_trapped(client):
    """Every earlier step stays reachable — changing your mind must not mean starting over."""
    created = client.post("/new", data={"pack": "support-replies", "name": "Support replies"})
    slug = created.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/w/{slug}/data", json={"filename": "e.csv", "content": _tickets_csv(300)})
    client.get(f"/w/{slug}/checks")
    client.post(f"/w/{slug}/checks", data={"check": ["similarity"]})

    from train.web import workspace

    w = workspace.load(slug)
    w.meta["stage"] = "done"
    w.save()
    # A finished model lands on its result, not on a progress page for a run that ended.
    assert client.get(f"/w/{slug}").headers["location"].endswith("/result")
    # And the earlier steps are still there.
    assert "Add your examples" in client.get(f"/w/{slug}?step=data").text
    assert "good answer look like" in client.get(f"/w/{slug}/checks").text
    assert client.get(f"/w/{slug}/again").status_code == 303
    assert workspace.load(slug).stage == "machines"
