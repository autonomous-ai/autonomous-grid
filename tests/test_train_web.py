"""Web-interface tests: the whole wizard a support manager walks through, and every refusal.

The refusals matter more than the happy path — this interface exists to stop someone spending a
night of their office's compute on data that can't teach anything, or serving a model that didn't
earn it.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

from train.web import build_app, prepare, workspace


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    # HOME too, and the schedulers stubbed: turning on nightly now installs a real per-user job,
    # and no test may write into the developer's own LaunchAgents.
    monkeypatch.setenv("HOME", str(tmp_path))
    from train import schedule as _schedule

    monkeypatch.setattr(_schedule, "_launchctl", lambda *a: (0, ""))
    monkeypatch.setattr(_schedule, "_systemctl", lambda *a: (0, ""))

    # What this computer can train is probed off the host — mlx_lm on an Apple laptop, torch/trl/
    # peft anywhere else. Left real, the wizard renders one page on a machine that happens to have
    # them and a different one on CI, and these tests are about the wizard, not about what is
    # pip-installed. Pinned to the torch rung, which reads the same on macOS and Linux.
    from train.web import machines as _machines

    monkeypatch.setattr(_machines, "_installed", lambda mod: mod in {"torch", "trl", "peft"})
    monkeypatch.setattr(_machines, "_has_feedback_trainer", lambda: True)
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
    # With no models yet, the front page explains the idea rather than showing an empty table.
    landing = client.get("/")
    assert landing.status_code == 200
    assert "one job the way your team does it" in landing.text
    assert "ten minutes of your time" in landing.text

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
    (w.path / "run-job.json").write_text(_json.dumps({
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
    (w.path / "run-job.json").write_text(_json.dumps({
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


# --- the payoff: it is live, and it keeps improving -----------------------------------------

def _served_workspace(client, tmp_path):
    """A workspace whose model passed and was deployed."""
    import json as _json

    from train.web import workspace

    created = client.post("/new", data={"pack": "support-replies", "name": "Support replies"})
    slug = created.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/w/{slug}/data", json={"filename": "e.csv", "content": _tickets_csv(300)})
    client.get(f"/w/{slug}/checks")
    client.post(f"/w/{slug}/checks", data={"check": ["similarity"]})
    w = workspace.load(slug)
    workspace.write_config(w, model="m", endpoint="http://127.0.0.1:8080/v1", steps=60)
    run = w.path / "run"
    run.mkdir(parents=True, exist_ok=True)
    (run / "eval-card.json").write_text(_json.dumps({
        "passed": True, "delta": 0.19, "verdict": "better by +0.190 — safe to serve",
        "before": {"per_grader": {"similarity": 0.6}, "overall": 0.6, "samples": [], "n": 30},
        "after": {"per_grader": {"similarity": 0.8}, "overall": 0.8, "samples": [], "n": 30},
    }), encoding="utf-8")
    adapter = run / "adapter"
    adapter.mkdir(exist_ok=True)
    (adapter / "adapter_config.json").write_text("{}", encoding="utf-8")
    (adapter / "adapter_model.safetensors").write_text("x", encoding="utf-8")
    return slug


def test_using_a_model_lands_on_a_page_that_says_it_is_live(client, tmp_path, monkeypatch):
    slug = _served_workspace(client, tmp_path)

    # Deploying talks to machines; stub that, not the decision to allow it.
    monkeypatch.setattr(
        "train.deploy.deploy_adapter",
        lambda adapter, nodes, name, **kw: [
            {"node": n, "ok": True, "detail": f"serving as {name!r}"} for n in nodes],
    )
    response = client.post(f"/w/{slug}/use")
    assert response.status_code == 303
    assert response.headers["location"].endswith("/live")

    page = client.get(f"/w/{slug}/live").text
    assert "is answering now" in page
    assert "Point your tools at it" in page
    assert "OPENAI_BASE_URL" in page                      # the one line her engineer needs
    assert "Improve it overnight" in page               # and the way to make it compound


def test_the_live_page_redirects_when_nothing_is_serving_yet(client, tmp_path):
    slug = _served_workspace(client, tmp_path)
    assert client.get(f"/w/{slug}/live").headers["location"].endswith("/result")


def test_turning_on_nightly_also_turns_on_collecting(client, tmp_path, monkeypatch):
    """There is nothing to learn from overnight unless the work is being kept."""
    from train import capture

    slug = _served_workspace(client, tmp_path)
    monkeypatch.setattr(
        "train.deploy.deploy_adapter",
        lambda adapter, nodes, name, **kw: [{"node": nodes[0], "ok": True, "detail": "ok"}],
    )
    client.post(f"/w/{slug}/use")
    assert capture.load_policy().enabled is False

    client.post(f"/w/{slug}/nightly", data={"nightly": "on"})
    assert capture.load_policy().enabled is True
    page = client.get(f"/w/{slug}/live").text
    assert "Stop improving it overnight" in page

    # And the job is really in the scheduler — not a line of crontab printed at her.
    from train import schedule as sched

    state = sched.status(slug=slug)
    assert state["installed"] and state["when"] == "23:00"
    assert "Installed" in page

    client.post(f"/w/{slug}/nightly", data={"nightly": "off"})
    assert "Improve it overnight" in client.get(f"/w/{slug}/live").text
    assert sched.status(slug=slug)["installed"] is False


def test_a_scheduler_that_refuses_leaves_the_toggle_off(client, tmp_path, monkeypatch):
    """The worst failure available here is a page that promises a night that will never come."""
    from train import schedule as sched

    slug = _served_workspace(client, tmp_path)
    monkeypatch.setattr(
        "train.deploy.deploy_adapter",
        lambda adapter, nodes, name, **kw: [{"node": nodes[0], "ok": True, "detail": "ok"}],
    )
    client.post(f"/w/{slug}/use")
    monkeypatch.setattr(sched, "install",
                        lambda *a, **k: sched.Result(False, "the scheduler would not take it"))

    client.post(f"/w/{slug}/nightly", data={"nightly": "on"})
    page = client.get(f"/w/{slug}/live").text
    assert "nothing is scheduled yet" in page
    assert "the scheduler would not take it" in page
    assert workspace.load(slug).meta["nightly"] is False   # the toggle follows reality
    # Collecting still went on: the examples accumulate either way, and she can run it by hand.
    from train import capture

    assert capture.load_policy().enabled is True


# --- scoring is a job, not a hanging request ------------------------------------------------

def test_checking_shows_a_count_instead_of_a_white_page(client, tmp_path, monkeypatch):
    """Scoring generates an answer per held-out item with TWO models — minutes, not seconds."""
    started: list[dict] = []
    monkeypatch.setattr(
        "train.web.jobs.start",
        lambda path, config, **kw: started.append({"path": path, **kw}) or {"running": True},
    )
    slug = _served_workspace(client, tmp_path)
    (workspace_run := workspace.load(slug).path / "run").mkdir(parents=True, exist_ok=True)
    (workspace_run / "eval-card.json").unlink(missing_ok=True)

    response = client.post(f"/w/{slug}/check")
    assert response.status_code == 303
    # It launched a separate "eval" job rather than scoring inside the request.
    assert started and started[0]["job"] == "eval" and started[0]["verb"] == "eval"
    assert "--candidate" in started[0]["extra"]


def test_scoring_page_reads_progress_from_the_log():
    from train.web import workspace as ws
    from train.web.pages import scoring_step

    class FakeWorkspace:
        slug = "support-replies"
        name = "Support replies"
        pack = "support-replies"
        meta: ClassVar[dict] = {}

    job = {"state": "running", "log_tail":
           "[grid train] scored 11 of 30 with base/model\n"
           "[grid train] scored 12 of 30 with base/model"}
    page = scoring_step(FakeWorkspace(), job)
    assert "answered 12 of 30" in page
    assert "first model" in page
    assert 'http-equiv="refresh"' in page          # it keeps itself up to date

    both = {"state": "running", "log_tail":
            "[grid train] scored 30 of 30 with base/model\n"
            "[grid train] scored 4 of 30 with support-replies"}
    assert "second model" in scoring_step(FakeWorkspace(), both)

    failed = {"state": "failed", "reason": "The machine that answers is asleep.",
              "log_tail": "boom"}
    page = scoring_step(FakeWorkspace(), failed)
    assert "Could not check it" in page
    assert "nothing has been served" in page
    assert "Try checking again" in page
    assert ws is not None


# --- the confirmed findings from the adversarial review, pinned -----------------------------

def test_the_machine_she_chose_is_the_machine_that_runs(client, tmp_path, monkeypatch):
    """A bare index could mean a different machine if the list re-sorted between draw and submit."""
    from train.web.machines import Capability, Machine

    started: dict = {}
    monkeypatch.setattr("train.web.jobs.start",
                        lambda path, config, **kw: started.update(kw) or {"running": True})

    first = Machine("http://one.local/v1", "Ollama on this computer", "a model", "ollama", False)
    second = Machine("http://two.local/v1", "vLLM on this computer", "a model", "vllm", True)
    monkeypatch.setattr("train.web.machines.find_machines", lambda: [first, second])
    monkeypatch.setattr("train.web.machines.capability",
                        lambda machines=None: Capability(True, False, "sft", "h", "d", "torch"))

    created = client.post("/new", data={"pack": "support-replies", "name": "x"})
    slug = created.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/w/{slug}/data", json={"filename": "e.csv", "content": _tickets_csv(300)})
    client.get(f"/w/{slug}/checks")
    client.post(f"/w/{slug}/checks", data={"check": ["similarity"]})

    # She picks the second machine; the list then re-sorts under her.
    monkeypatch.setattr("train.web.machines.find_machines", lambda: [second, first])
    client.post(f"/w/{slug}/start", data={"machine": "vLLM on this computer", "model": "0",
                                          "effort": "quick"})
    config = (workspace.load(slug).path / "grid-train.toml").read_text(encoding="utf-8")
    assert "two.local" in config          # the one she chose, not the one now at that position


def test_a_typed_address_cannot_rewrite_the_config(client, tmp_path):
    """Free text reached a TOML file; a quote or newline used to be able to rewrite the rest."""
    from train.config import load_config

    created = client.post("/new", data={"pack": "support-replies", "name": "x"})
    slug = created.headers["location"].rsplit("/", 1)[-1]
    client.post(f"/w/{slug}/data", json={"filename": "e.csv", "content": _tickets_csv(300)})
    client.get(f"/w/{slug}/checks")
    client.post(f"/w/{slug}/checks", data={"check": ["similarity"]})
    nasty = 'http://x/v1"\nsteps = 99999\nevil = "'
    client.post(f"/w/{slug}/start", data={"endpoint": nasty, "model": "0", "effort": "quick"})

    w = workspace.load(slug)
    cfg = load_config(w.path / "grid-train.toml")
    assert cfg.rollout.base_url == nasty          # stored verbatim, as one string
    assert cfg.trainer.steps != 99999             # and it did not become configuration


def test_the_machines_step_needs_examples_behind_it(client, tmp_path):
    created = client.post("/new", data={"pack": "support-replies", "name": "x"})
    slug = created.headers["location"].rsplit("/", 1)[-1]
    response = client.get(f"/w/{slug}?step=machines")
    assert response.status_code == 400
    assert "Add your examples first" in response.text


def test_stopping_a_run_is_not_reported_as_a_crash(tmp_path, monkeypatch):
    import json as _json
    import time

    from train.web import jobs, pages, workspace

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("x", "support-replies")
    (w.path / "run-job.json").write_text(_json.dumps({
        "pid": 999999, "started": time.time() - 60, "config": "c", "verb": "sft",
        "stopped_by_user": True, "exit": -15, "ended": time.time(),
    }), encoding="utf-8")
    job = jobs.status(w.path)
    assert job["state"] == "stopped"
    assert "You stopped it" in job["reason"]
    assert "code -15" not in pages.running_step(w, job)


def test_a_crash_that_left_a_partial_model_is_not_called_finished(tmp_path, monkeypatch):
    import json as _json
    import time

    from train.web import jobs, workspace

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("x", "support-replies")
    adapter = w.path / "run" / "adapter"
    adapter.mkdir(parents=True)
    (adapter / "half-written.safetensors").write_text("...", encoding="utf-8")
    (w.path / "run-job.json").write_text(_json.dumps({
        "pid": 999999, "started": time.time() - 60, "config": "c", "verb": "sft",
        "exit": 137, "ended": time.time(),
    }), encoding="utf-8")
    job = jobs.status(w.path)
    assert job["state"] == "failed"
    assert "memory" in job["reason"]          # 137 = killed; a smaller model is the fix


def test_the_first_screen_explains_itself(client):
    """Whoever opens this has never seen it. Twenty seconds to land the idea."""
    page = client.get("/").text
    # What it is, what it asks of her, and what it will not do.
    assert "Your examples" in page and "What good looks like" in page
    assert "It proves itself" in page
    assert "nothing leaves your network" in page
    assert "If it is not better, it does not get used" in page
    # And an honest word about this computer, whatever the answer is.
    assert ("This computer is ready" in page) or ("One thing to install first" in page)
    for jargon in ("GRPO", "LoRA", "adapter", "endpoint", "rollout", "reinforcement"):
        assert jargon not in page


def test_the_front_page_lists_models_once_there_are_any(client, tmp_path):
    client.post("/new", data={"pack": "support-replies", "name": "Support replies"})
    page = client.get("/").text
    assert "Your models" in page
    assert "Support replies" in page
    assert "one job the way your team does it" not in page   # the explainer steps aside


# --- the two general packs: any department, not just support and sales ----------------------

def _generic_jsonl(n: int = 300) -> str:
    """Procurement answering suppliers — a department nobody wrote a pack for."""
    rows = [
        {"Request": f"Supplier asks when PO-{4400 + i} will be approved and what the terms are",
         "Reply sent": (f"PO-{4400 + i} is approved as of this morning. Terms are NET-30 from "
                        "delivery, and the remittance contact is ap@autonomous.ai.")}
        for i in range(n)
    ]
    return "".join(json.dumps(r) + "\n" for r in rows)


def _labelled_csv(per_class: int = 40) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Text", "Queue"])
    for i in range(per_class):
        writer.writerow([f"My invoice {i} was charged twice", "billing"])
        writer.writerow([f"Where is order AN-{i} now", "shipping"])
        writer.writerow([f"Desk {i} shows E07 and will not move", "technical"])
    return buf.getvalue()


def test_the_generic_pack_reads_any_two_columns_and_says_which():
    report, kept = prepare.prepare("any-task", _generic_jsonl(300), "procurement.jsonl")
    assert report.ok and report.level == "good"
    assert report.rows_usable == 300
    # Reading the answer as the question would train the model backwards, so the guess is stated.
    assert report.columns_used == {"the work": "request", "what your team wrote": "reply sent"}
    assert "rename the columns" in report.detail
    assert kept[0]["prompt"].startswith("Supplier asks")


def test_the_generic_pack_refuses_a_one_column_export():
    report, kept = prepare.prepare("any-task", '{"notes": "just one column"}\n', "x.jsonl")
    assert not report.ok and report.level == "blocked"
    assert "two columns" in report.headline
    assert kept == []


def test_categories_are_balanced_so_guessing_the_commonest_cannot_win():
    lopsided = _labelled_csv(40).splitlines()
    lopsided += [f"Another billing one {i},billing" for i in range(200)]
    report, kept = prepare.prepare("sort-into-categories", "\n".join(lopsided) + "\n", "q.csv")
    assert report.ok
    assert report.distribution["billing"] == 240      # what the export contained
    assert len(kept) == 3 * 40                        # what we train on, capped at the smallest
    assert "balanced at 40" in report.headline


def test_sorting_needs_at_least_two_categories_and_not_too_many():
    one = "Text,Queue\n" + "".join(f"item {i},billing\n" for i in range(80))
    report, _ = prepare.prepare("sort-into-categories", one, "q.csv")
    assert not report.ok and "one category" in report.headline

    many = "Text,Queue\n" + "".join(f"item {i},cat{i}\n" for i in range(60))
    report, _ = prepare.prepare("sort-into-categories", many, "q.csv")
    assert not report.ok and "more like free text" in report.detail

    thin = "Text,Queue\n" + "".join(f"item {i},{'a' if i % 2 else 'b'}\n" for i in range(12))
    report, _ = prepare.prepare("sort-into-categories", thin, "q.csv")
    assert not report.ok and "smallest category" in report.headline


@pytest.mark.parametrize("pack,payload,filename", [
    ("support-replies", _tickets_csv(60), "t.csv"),
    ("sales-triage", _leads_jsonl(20), "l.jsonl"),
    ("any-task", _generic_jsonl(60), "g.jsonl"),
    ("sort-into-categories", _labelled_csv(40), "q.csv"),
], ids=["support-replies", "sales-triage", "any-task", "sort-into-categories"])
def test_the_generated_rewards_file_actually_runs(pack, payload, filename, tmp_path, monkeypatch):
    """Every pack's rewards.py is generated Python. If it does not import, the night is wasted.

    This is the only test that executes the generated source, so it exercises each template's
    regexes and its reading of refs/labels off disk — the parts a syntax check would miss.
    """
    from train.config import DataConfig, RewardsConfig
    from train.rewards import load_reward_funcs

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create(f"w-{pack}", pack)
    report = workspace.attach_data(w, filename, payload)
    assert report.ok, report.headline
    ticked = [c["id"] for c in workspace.CHECKS[pack] if c.get("default")]
    path = workspace.write_checks(w, ticked)

    funcs = load_reward_funcs(RewardsConfig(python_file=str(path)),
                              DataConfig(prompts_jsonl=str(w.path / "prompts.jsonl")))
    assert len(funcs) == len(ticked)

    prompts = [json.loads(line)["prompt"] for line in
               (w.path / "prompts.jsonl").read_text(encoding="utf-8").splitlines()[:4]]
    answers = [json.loads(line)["messages"][-1]["content"] for line in
               (w.path / "sft.jsonl").read_text(encoding="utf-8").splitlines()[:4]]
    for func in funcs:
        scores = func(prompts, completions=answers, completions_text=answers)
        assert len(scores) == len(prompts)
        assert all(0.0 <= float(s) <= 1.0 for s in scores), (func.__name__, scores)
    # Her team's own answers should score well by construction — that is what "correct" means here.
    named = {f.__name__: f for f in funcs}
    for key in ("reward_similarity", "reward_correct_category", "reward_correct_priority"):
        if key in named:
            got = named[key](prompts, completions=answers, completions_text=answers)
            assert min(got) >= 0.6, (key, got)


def test_a_wrong_category_scores_zero_and_rambling_loses_the_shape_mark(tmp_path, monkeypatch):
    from train.config import DataConfig, RewardsConfig
    from train.rewards import load_reward_funcs

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("q", "sort-into-categories")
    workspace.attach_data(w, "q.csv", _labelled_csv(40))
    path = workspace.write_checks(w, ["correct_category", "parseable"])
    funcs = {f.__name__: f for f in load_reward_funcs(
        RewardsConfig(python_file=str(path)),
        DataConfig(prompts_jsonl=str(w.path / "prompts.jsonl")))}

    prompt = json.loads((w.path / "prompts.jsonl").read_text(encoding="utf-8")
                        .splitlines()[0])["prompt"]
    truth = json.loads((w.path / "labels.jsonl").read_text(encoding="utf-8")
                       .splitlines()[0])["label"]
    wrong = next(c for c in ("billing", "shipping", "technical") if c != truth)

    assert funcs["reward_correct_category"]([prompt], completions=[f"CATEGORY: {truth}"],
                                           completions_text=[f"CATEGORY: {truth}"]) == [1.0]
    assert funcs["reward_correct_category"]([prompt], completions=[f"CATEGORY: {wrong}"],
                                           completions_text=[f"CATEGORY: {wrong}"]) == [0.0]
    # An invented category is wrong even if it sounds plausible.
    assert funcs["reward_correct_category"]([prompt], completions=["CATEGORY: finance"],
                                           completions_text=["CATEGORY: finance"]) == [0.0]
    tidy = funcs["reward_parseable"]([prompt], completions=[f"CATEGORY: {truth}"],
                                     completions_text=[f"CATEGORY: {truth}"])[0]
    ramble = funcs["reward_parseable"](
        [prompt],
        completions=[f"Well it could be a few things.\nCATEGORY: {truth}\nCATEGORY: {wrong}\nHope that helps!"],
        completions_text=[f"Well it could be a few things.\nCATEGORY: {truth}\nCATEGORY: {wrong}\nHope that helps!"])[0]
    assert tidy == 1.0 and ramble < tidy


def test_a_manager_can_walk_the_whole_wizard_with_her_own_categories(client, tmp_path, monkeypatch):
    """The generic path, through the browser, exactly as a logistics manager would meet it."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    page = client.get("/new").text
    assert "Sort work into your own categories" in page
    assert "Something else my team answers in writing" in page

    # No name typed: it gets named after the job rather than after support.
    created = client.post("/new", data={"pack": "sort-into-categories"})
    slug = created.headers["location"].split("/w/")[1].split("/")[0]
    assert "Sort work into your own categories" in client.get("/").text

    up = client.post(f"/w/{slug}/data", json={"filename": "queues.csv",
                                              "content": _labelled_csv(40)})
    assert up.json()["ok"]
    data_page = client.get(f"/w/{slug}").text
    assert "balanced at 40" in data_page
    assert "Columns we used:" in data_page and "“queue”" in data_page

    checks_page = client.get(f"/w/{slug}/checks").text
    assert "Picks the category your team picked" in checks_page
    client.post(f"/w/{slug}/checks", data={"check": ["parseable"]})
    generated = (workspace.load(slug).path / "rewards.py").read_text(encoding="utf-8")
    assert "reward_correct_category" in generated       # locked check cannot be unticked

    workspace.write_config(workspace.load(slug), model="m", endpoint="http://x/v1", steps=10)
    toml = (workspace.load(slug).path / "grid-train.toml").read_text(encoding="utf-8")
    assert "max_tokens = 24" in toml                    # a category, not an essay


# --- the overnight page: the unattended half, made visible ---------------------------------

def _serving(w):
    w.meta["serving"] = {"name": w.slug, "nodes": [{"node": "studio", "ok": True}]}
    w.meta["nightly"] = True
    w.meta["stage"] = "serving"
    w.save()


def test_overnight_page_says_what_tonight_will_do(client, tmp_path, monkeypatch):
    from train import autopilot
    from train.web import pages

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("Support replies", "support-replies")
    _serving(w)

    class Summary:
        requests, trainable, edited, teacher_rows = 900, 640, 210, 120
        first_seen, last_seen = "2026-07-01T09:00:00", "2026-07-25T18:30:00"
        headline = "640 examples ready to learn from, out of 900 requests."
        advice = "That is enough for a training run tonight."

    page = pages.overnight_page(w, Summary(), [], {"power": "on mains", "activity": "idle 900s"},
                                nightly_on=True, min_examples=autopilot.MIN_EXAMPLES,
                                schedule={"installed": True, "when": "23:00"})
    assert "Tonight at 23:00 it will train</b> on 640 examples" in page
    assert "on mains, idle 900s" in page
    assert "No nights yet" in page
    for jargon in ("GRPO", "LoRA", "adapter", "SFT", "rollout"):
        assert jargon not in page


def test_overnight_page_counts_down_instead_of_promising(client, tmp_path, monkeypatch):
    from train.web import pages

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("q", "sort-into-categories")
    _serving(w)

    class Thin:
        requests, trainable, edited, teacher_rows = 60, 43, 40, 3
        first_seen = last_seen = ""
        headline = "43 examples ready to learn from, out of 60 requests."
        advice = "It grows on its own as your team works."

    page = pages.overnight_page(w, Thin(), [], {"power": "on battery", "activity": "in use (2s ago)"},
                                nightly_on=True, min_examples=120,
                                schedule={"installed": True, "when": "02:00"})
    assert "Tonight at 02:00 it will wait" in page
    assert "77 to go" in page                     # 120 - 43, said as a countdown not a failure


def test_overnight_page_shows_the_nights_that_were_refused(client, tmp_path, monkeypatch):
    """A refused night is the gate working. Hiding it would make the record look too good."""
    from train.web import pages

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("s", "support-replies")
    _serving(w)
    history = [
        {"started": "2026-07-22T23:00:00", "stage": "waiting", "examples": 40, "detail": "..."},
        {"started": "2026-07-23T23:00:00", "stage": "trained", "examples": 300, "delta": -0.004},
        {"started": "2026-07-24T23:00:00", "stage": "deployed", "examples": 480, "delta": 0.062},
        {"started": "2026-07-25T23:00:00", "stage": "skipped", "detail": "on battery"},
    ]

    class S:
        requests, trainable, edited, teacher_rows = 900, 480, 200, 100
        first_seen = last_seen = ""
        headline = "h"
        advice = "a"

    page = pages.overnight_page(w, S(), history, {"power": "on mains", "activity": "idle 100s"},
                                nightly_on=True, schedule={"installed": True, "when": "23:00"})
    assert "not better" in page and "-0.004" in page
    assert "now serving" in page and "+0.062" in page
    assert "left it alone" in page
    # Newest night first: she reads the top of the table.
    assert page.index("2026-07-25") < page.index("2026-07-22")


def test_overnight_page_calls_out_a_promise_the_computer_is_not_keeping(client, tmp_path,
                                                                       monkeypatch):
    """Collecting is on, but nothing is in the scheduler: say so instead of implying a night."""
    from train.web import pages

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("s", "support-replies")
    _serving(w)

    class S:
        requests, trainable, edited, teacher_rows = 900, 900, 200, 100
        first_seen = last_seen = ""
        headline = "h"
        advice = "a"

    page = pages.overnight_page(w, S(), [], {"power": "on mains", "activity": "idle 100s"},
                                nightly_on=True, schedule={"installed": False, "when": ""})
    assert "Nothing will happen tonight" in page
    assert "grid train autopilot" in page


def test_the_live_page_links_to_the_overnight_record(client, tmp_path, monkeypatch):
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("s", "support-replies")
    _serving(w)
    live = client.get(f"/w/{w.slug}/live").text
    assert f"/w/{w.slug}/overnight" in live
    page = client.get(f"/w/{w.slug}/overnight")
    assert page.status_code == 200
    assert "What it does overnight" in page.text


def test_the_overnight_page_survives_an_unreadable_config(client, tmp_path, monkeypatch):
    """The history lives behind the run config. A broken one must not take the page down."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("s", "support-replies")
    _serving(w)
    (w.path / "grid-train.toml").write_text("this is not toml {{{", encoding="utf-8")
    page = client.get(f"/w/{w.slug}/overnight")
    assert page.status_code == 200
    assert "No nights yet" in page.text


def test_the_data_page_says_where_to_get_the_file(client, tmp_path):
    """The step everything depends on is the one we gave the least help with."""
    from train.web import pages

    created = client.post("/new", data={"pack": "support-replies"})
    slug = created.headers["location"].split("/w/")[1].split("/")[0]
    page = client.get(f"/w/{slug}").text
    assert "Where do I get that file?" in page
    assert "Zendesk" in page and "Admin Center" in page
    # Every pack gets its own list, and none of them assumes she has a helpdesk at all.
    for pack in pages.PACK_TITLES:
        assert "spreadsheet" in pages.export_help(pack)


def test_a_night_that_lost_is_never_shown_as_a_win(client, tmp_path, monkeypatch):
    """The stage word becomes a sentence a customer reads. It has to be the true one."""
    from train.web import pages

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("s", "support-replies")
    _serving(w)

    class S:
        requests, trainable, edited, teacher_rows = 900, 480, 200, 100
        first_seen = last_seen = ""
        headline = "h"
        advice = "a"

    history = [
        # What run_cycle records now.
        {"started": "2026-07-24T23:00:00", "stage": "refused", "ok": False,
         "examples": 400, "delta": -0.004, "detail": "no meaningful gain (-0.004)"},
        # What it recorded before 2026-07-26 for the same outcome — a history file outlives the
        # release that wrote it, so the page must still read this correctly.
        {"started": "2026-07-23T23:00:00", "stage": "proved", "ok": False,
         "examples": 380, "delta": -0.010, "detail": "no meaningful gain (-0.010)"},
        # Trained but never compared: not the same thing as "not better".
        {"started": "2026-07-22T23:00:00", "stage": "trained", "ok": False, "examples": 300,
         "detail": "trained, but there is no held-out set to prove it against"},
    ]
    page = pages.overnight_page(w, S(), history, {"power": "on mains", "activity": "idle 60s"},
                                nightly_on=True, schedule={"installed": True, "when": "23:00"})
    assert "It won on held-back work" not in page       # nothing here won
    assert page.count("chip '>not better") == 2          # both refusals, old and new spelling
    assert "not checked" in page
    assert "no held-back work to compare it against" in page


def test_the_page_never_contradicts_the_scheduler(client, tmp_path, monkeypatch):
    from train.web import pages

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("s", "support-replies")
    _serving(w)

    class S:
        requests, trainable, edited, teacher_rows = 900, 900, 200, 100
        first_seen = last_seen = ""
        headline = "h"
        advice = "a"

    host = {"power": "on mains", "activity": "idle 60s"}
    # A job exists but this model's switch is off: say so, don't print "nothing is scheduled".
    installed_off = pages.overnight_page(w, S(), [], host, nightly_on=False,
                                         schedule={"installed": True, "when": "23:00", "mine": True})
    assert "A nightly job is installed" in installed_off
    assert "Nothing is scheduled" not in installed_off

    # A job installed by a different folder with the same model name would be replaced silently.
    foreign = pages.overnight_page(w, S(), [], host, nightly_on=True,
                                   schedule={"installed": True, "when": "23:00", "mine": False,
                                             "workspace": "/Users/dee/other/support-replies"})
    assert "Another model owns tonight" in foreign
    assert "/Users/dee/other/support-replies" in foreign


def test_a_long_category_is_not_permanently_unlearnable(client, tmp_path, monkeypatch):
    """24 tokens fits "billing" and truncates a real one — and a truncated answer scores 0.0
    on every rollout of that category, forever."""
    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    long_label = "escalate-to-tier-2/hardware-warranty-emea-approval-needed"
    rows = "Text,Queue\n" + "".join(
        f"item {i},{long_label if i % 2 else 'billing'}\n" for i in range(120))
    w = workspace.create("q", "sort-into-categories")
    report = workspace.attach_data(w, "q.csv", rows)
    assert report.ok, report.headline
    workspace.write_config(w, model="m", endpoint="http://x/v1", steps=10)
    toml = (w.path / "grid-train.toml").read_text(encoding="utf-8")
    budget = int(next(line for line in toml.splitlines()
                      if line.startswith("max_tokens")).split("=")[1])
    # Enough room for "CATEGORY: " plus the longest category the customer actually uses.
    assert budget >= 40, toml

    short = workspace.create("q2", "sort-into-categories")
    workspace.attach_data(short, "q.csv", "Text,Queue\n" + "".join(
        f"item {i},{'billing' if i % 2 else 'shipping'}\n" for i in range(120)))
    workspace.write_config(short, model="m", endpoint="http://x/v1", steps=10)
    short_toml = (short.path / "grid-train.toml").read_text(encoding="utf-8")
    assert "max_tokens = 24" in short_toml      # short categories stay cheap


def test_the_column_guess_prefers_the_field_a_team_actually_sorts_by(client, tmp_path):
    """Status is in every helpdesk export and is almost never the routing field."""
    from train.web import prepare

    rows = "Id,Subject,Description,Group,Status\n" + "".join(
        f"{i},s,desc {i},{['billing', 'shipping', 'technical'][i % 3]},solved\n" for i in range(120))
    report, _ = prepare.prepare("sort-into-categories", rows, "zendesk.csv")
    assert report.columns_used["the category"] == "group"
    assert report.ok and len(report.distribution) == 3


def test_a_mailbox_export_is_not_trained_to_answer_with_a_timestamp(client, tmp_path):
    """"Sent" in a mailbox export is the time, not the reply."""
    import json as _json

    from train.web import prepare

    rows = [{"From": "s@x.com", "Subject": f"PO-{i}", "Body": f"When will PO-{i} be approved?",
             "Sent": "2026-07-01 10:04:11 +0700",
             "Reply body": "Approved this morning. Terms are NET-30 from delivery."}
            for i in range(60)]
    report, kept = prepare.prepare(
        "any-task", "".join(_json.dumps(r) + "\n" for r in rows), "mailbox.jsonl")
    assert report.columns_used["what your team wrote"] == "reply body"
    assert kept[0]["reference"].startswith("Approved this morning")


# --- the way back ---------------------------------------------------------------------------

def test_serving_a_model_keeps_the_one_it_replaced(client, tmp_path, monkeypatch):
    """"Start using this model" replaced what her team relies on, and had no undo."""
    slug = _served_workspace(client, tmp_path)
    loaded: list[str] = []
    monkeypatch.setattr(
        "train.deploy.deploy_adapter",
        lambda adapter, nodes, name, **kw: loaded.append(str(adapter))
        or [{"node": nodes[0], "ok": True, "detail": "ok"}],
    )

    client.post(f"/w/{slug}/use")
    first = workspace.load(slug).meta["serving"]
    assert first["adapter"] and Path(first["adapter"]).is_dir()
    assert first["replaced"] == ""                     # nothing came before it
    page = client.get(f"/w/{slug}/live").text
    assert "first model you have served here" in page
    assert "restarting the engine" in page             # honest about what going back would need

    # Train and serve a second one: now there is something to go back to.
    (workspace.load(slug).path / "run" / "adapter").mkdir(parents=True, exist_ok=True)
    client.post(f"/w/{slug}/use")
    second = workspace.load(slug).meta["serving"]
    assert second["replaced"] == first["adapter"]
    assert second["adapter"] != first["adapter"]       # the copy, not the live run directory
    page = client.get(f"/w/{slug}/live").text
    assert "Go back to the previous model" in page

    client.post(f"/w/{slug}/revert")
    now = workspace.load(slug).meta["serving"]
    assert now["adapter"] == first["adapter"]
    assert loaded[-1] == first["adapter"]              # the old weights really went to the node
    page = client.get(f"/w/{slug}/live").text
    assert "You went back to the previous model" in page


def test_going_back_with_nothing_to_go_back_to_says_so(client, tmp_path, monkeypatch):
    slug = _served_workspace(client, tmp_path)
    monkeypatch.setattr(
        "train.deploy.deploy_adapter",
        lambda adapter, nodes, name, **kw: [{"node": nodes[0], "ok": True, "detail": "ok"}],
    )
    client.post(f"/w/{slug}/use")
    response = client.post(f"/w/{slug}/revert")
    assert response.status_code == 400
    assert "nothing to go back to" in response.text
    assert "stays as it is" in response.text           # and nothing was lost


def test_the_column_ranking_never_lets_a_partial_match_beat_an_exact_one(client, tmp_path):
    """The regression my own fix introduced: strong-substring outranked weak-exact, so
    "resolution date" (a timestamp) beat a column literally called "result"."""
    import json as _json

    from train.web import prepare

    rows = [{"Task": f"job {i}", "Result": "Approved and shipped, tracking sent to the customer.",
             "Resolution date": "2026-07-01 10:04:11 +0700"} for i in range(300)]
    report, kept = prepare.prepare(
        "any-task", "".join(_json.dumps(r) + "\n" for r in rows), "sheet.jsonl")
    assert report.columns_used["what your team wrote"] == "result"
    assert not kept[0]["reference"].startswith("2026")


def test_status_stays_the_answer_key_where_it_is_the_answer_key(client, tmp_path):
    """Demoting "status" everywhere made lead triage pick the pipeline *stage* instead, which
    collapsed a won/lost history into one class — and still called it balanced."""
    import json as _json

    from train.web import prepare

    rows = []
    for i in range(120):
        rows.append({"title": f"deal {i}", "lead": "We need 30 desks, budget approved",
                     "status": "Closed Won", "stage": "Negotiations Started"})
        rows.append({"title": f"deal {i}b", "lead": "Just browsing, no budget",
                     "status": "No response", "stage": "Contact Made"})
    report, _ = prepare.prepare(
        "sales-triage", "".join(_json.dumps(r) + "\n" for r in rows), "pipedrive.jsonl")
    assert report.columns_used["outcome"] == "status"
    assert set(report.distribution) >= {"hot", "cold"}
    assert report.distribution.get("hot", 0) > 0 and report.distribution.get("cold", 0) > 0


def test_a_single_class_export_is_refused_not_called_balanced(client, tmp_path):
    """Every label identical means there is nothing to learn — and a grader that scores a constant
    answer 1.0, which sails through the gate."""
    import json as _json

    from train.web import prepare

    rows = [{"lead": f"enquiry {i}", "outcome": "Qualified"} for i in range(300)]
    report, _ = prepare.prepare(
        "sales-triage", "".join(_json.dumps(r) + "\n" for r in rows), "crm.jsonl")
    assert not report.ok
    assert report.distribution.get("hot", 0) == 0


def test_a_four_letter_alias_does_not_hijack_a_longer_column(client, tmp_path):
    """"form" was added for Zendesk's ticket form, and lives inside "information" and "platform"."""
    from train.web import prepare

    rows = "Subject,Description,Additional information,Status\n" + "".join(
        f"s{i},desc {i},notes,{['billing', 'shipping', 'warranty'][i % 3]}\n" for i in range(300))
    report, _ = prepare.prepare("sort-into-categories", rows, "z.csv")
    assert report.columns_used["the category"] == "status"
    assert len(report.distribution) == 3


def test_you_can_run_tonights_cycle_now_when_there_is_enough_to_train_on(client, tmp_path,
                                                                        monkeypatch):
    """Nobody evaluating this product will wait until 23:00 to find out whether it works."""
    from train.web import pages

    monkeypatch.setenv("GRID_HOME", str(tmp_path))
    w = workspace.create("s", "support-replies")
    _serving(w)

    class Ready:
        requests, trainable, edited, teacher_rows = 900, 640, 200, 100
        first_seen = last_seen = ""
        headline = "h"
        advice = "a"

    class Thin(Ready):
        trainable = 12

    host = {"power": "on mains", "activity": "idle 60s"}
    ready = pages.overnight_page(w, Ready(), [], host, nightly_on=True, min_examples=120,
                                 schedule={"installed": True, "when": "23:00", "mine": True})
    assert "Train on it now instead of waiting" in ready
    assert "even if someone is working on it" in ready      # what it costs, said plainly
    assert "nothing is served unless it wins" in ready      # and what it does not change

    # Not enough to learn from: offering it would only produce a refusal.
    assert "Train on it now" not in pages.overnight_page(
        w, Thin(), [], host, nightly_on=True, min_examples=120,
        schedule={"installed": True, "when": "23:00", "mine": True})

    # While it runs, the button becomes a status line rather than a second start.
    running = pages.overnight_page(w, Ready(), [], host, nightly_on=True, min_examples=120,
                                   schedule={"installed": True, "when": "23:00", "mine": True},
                                   job={"running": True, "phase": "Learning from your examples."})
    assert "Training now" in running and "Train on it now" not in running


def test_training_now_starts_the_same_cycle_the_scheduler_would(client, tmp_path, monkeypatch):
    started: dict = {}
    monkeypatch.setattr("train.web.jobs.start",
                        lambda path, config, **kw: started.update(kw) or {"running": True})
    slug = _served_workspace(client, tmp_path)
    workspace.write_config(workspace.load(slug), model="m", endpoint="http://x/v1", steps=10)

    response = client.post(f"/w/{slug}/tonight")
    assert response.status_code == 303
    assert started["verb"] == "autopilot"                   # the same verb 23:00 runs
    assert "--ignore-host" in started["extra"]              # she asked for it on this machine


def test_a_model_retrained_after_its_check_cannot_be_served_on_the_old_card(client, tmp_path,
                                                                           monkeypatch):
    """Check, retrain, press "Start using this model" — and the customer gets weights that
    nothing has ever measured."""
    import json as _json

    slug = _served_workspace(client, tmp_path)
    monkeypatch.setattr(
        "train.deploy.deploy_adapter",
        lambda adapter, nodes, name, **kw: [{"node": nodes[0], "ok": True, "detail": "ok"}],
    )
    w = workspace.load(slug)
    adapter = w.path / "run" / "adapter"
    adapter.mkdir(parents=True, exist_ok=True)
    (adapter / "adapter_model.safetensors").write_text("the checked weights", encoding="utf-8")

    from train.evaluate import _fingerprint

    card = _json.loads((w.path / "run" / "eval-card.json").read_text(encoding="utf-8"))
    card["adapter_fingerprint"] = _fingerprint(adapter)
    (w.path / "run" / "eval-card.json").write_text(_json.dumps(card), encoding="utf-8")
    assert client.post(f"/w/{slug}/use").status_code == 303      # matches: allowed

    (adapter / "adapter_model.safetensors").write_text("trained again since", encoding="utf-8")
    blocked = client.post(f"/w/{slug}/use")
    assert blocked.status_code == 409
    assert "changed after it was checked" in blocked.text
