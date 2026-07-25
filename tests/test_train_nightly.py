"""Nightly-cycle tests: the order of operations, and every refusal along the way.

The value of this loop is entirely in what it refuses to do — take a laptop someone is using,
ship a model that didn't win, or claim success when the deploy half-failed. Each of those is a
test here.
"""
from __future__ import annotations

import json

import pytest

from train import nightly
from train.config import (
    DataConfig,
    DeployConfig,
    RewardsConfig,
    RolloutConfig,
    TrainerConfig,
    TrainRunConfig,
)


def _cfg(tmp_path, adapter_name="support-v1") -> TrainRunConfig:
    return TrainRunConfig(
        model_name="base/model",
        rollout=RolloutConfig(base_url="http://grid.test/v1"),
        data=DataConfig(prompts_jsonl=str(tmp_path / "p.jsonl")),
        rewards=RewardsConfig(python_file=str(tmp_path / "r.py")),
        trainer=TrainerConfig(output_dir=str(tmp_path / "out")),
        deploy=DeployConfig(nodes=("http://node.test/v1",), adapter_name=adapter_name),
        source_path=tmp_path / "grid-train.toml",
    )


@pytest.fixture
def stubs(tmp_path, monkeypatch):
    """Stand in for the three heavy steps so the ORDER and the GATE are what's under test."""
    calls: list[str] = []
    adapter = tmp_path / "out" / "run-1" / "adapter"
    adapter.mkdir(parents=True)

    def fake_train(cfg):
        calls.append("train")
        return adapter

    monkeypatch.setattr("train.run.run_training", fake_train)
    monkeypatch.setattr(nightly, "host_is_free", lambda **kw: (True, "idle"))
    return {"calls": calls, "adapter": adapter, "tmp": tmp_path}


def _eval_returning(passed: bool, delta: float, monkeypatch, calls):
    def fake_eval(cfg, run_dir, candidate, **kwargs):
        calls.append(f"eval:{candidate}")
        return {"passed": passed, "delta": delta,
                "verdict": "better by +0.2" if passed else "no meaningful gain (+0.00)"}

    monkeypatch.setattr("train.evaluate.run_eval", fake_eval)


def _deploy_returning(ok: bool, monkeypatch, calls):
    def fake_deploy(adapter_dir, nodes, name, **kwargs):
        calls.append(f"deploy:{name}")
        return [{"node": n, "ok": ok, "detail": "serving" if ok else "unreachable"} for n in nodes]

    monkeypatch.setattr("train.deploy.deploy_adapter", fake_deploy)


def test_happy_night_trains_proves_then_deploys_in_that_order(stubs, monkeypatch):
    calls = stubs["calls"]
    _eval_returning(True, 0.2, monkeypatch, calls)
    _deploy_returning(True, monkeypatch, calls)
    result = nightly.run_cycle(_cfg(stubs["tmp"]))
    assert result.ok and result.stage == "deployed"
    assert calls == ["train", "eval:support-v1", "deploy:support-v1"]
    assert "now serving as support-v1" in result.detail


def test_a_model_that_did_not_win_is_never_deployed(stubs, monkeypatch):
    calls = stubs["calls"]
    _eval_returning(False, 0.004, monkeypatch, calls)
    _deploy_returning(True, monkeypatch, calls)
    result = nightly.run_cycle(_cfg(stubs["tmp"]))
    assert not result.ok and result.stage == "proved"
    assert "deploy:support-v1" not in calls      # the gate held
    assert "no meaningful gain" in result.detail


def test_a_failed_load_is_reported_not_claimed_as_success(stubs, monkeypatch):
    calls = stubs["calls"]
    _eval_returning(True, 0.3, monkeypatch, calls)
    _deploy_returning(False, monkeypatch, calls)
    result = nightly.run_cycle(_cfg(stubs["tmp"]))
    assert not result.ok
    assert "loading it failed" in result.detail


def test_no_deploy_flag_stops_before_serving(stubs, monkeypatch):
    calls = stubs["calls"]
    _eval_returning(True, 0.3, monkeypatch, calls)
    _deploy_returning(True, monkeypatch, calls)
    result = nightly.run_cycle(_cfg(stubs["tmp"]), deploy=False)
    assert result.ok and result.stage == "proved"
    assert not any(c.startswith("deploy") for c in calls)


def test_a_machine_in_use_is_left_alone(stubs, monkeypatch):
    monkeypatch.setattr(nightly, "host_is_free",
                        lambda **kw: (False, "someone is using this machine"))
    result = nightly.run_cycle(_cfg(stubs["tmp"]))
    assert result.ok and result.stage == "skipped"   # skipping is a success, not a failure
    assert stubs["calls"] == []                      # nothing was started


def test_training_failure_is_reported_without_a_traceback(stubs, monkeypatch):
    def boom(cfg):
        raise SystemExit("grid train: missing training dependency (torch)")

    monkeypatch.setattr("train.run.run_training", boom)
    result = nightly.run_cycle(_cfg(stubs["tmp"]))
    assert not result.ok and result.stage == "failed"
    assert "missing training dependency" in result.detail


def test_every_night_is_appended_to_history(stubs, monkeypatch):
    calls = stubs["calls"]
    _eval_returning(True, 0.2, monkeypatch, calls)
    _deploy_returning(True, monkeypatch, calls)
    cfg = _cfg(stubs["tmp"])
    nightly.run_cycle(cfg)
    monkeypatch.setattr(nightly, "host_is_free", lambda **kw: (False, "on battery"))
    nightly.run_cycle(cfg)
    rows = nightly.history(cfg)
    assert [r["stage"] for r in rows] == ["deployed", "skipped"]
    written = (stubs["tmp"] / "out" / "nightly.jsonl").read_text(encoding="utf-8")
    assert len(written.strip().splitlines()) == 2
    assert json.loads(written.splitlines()[0])["ok"] is True


# --- host signals: unknown must never veto -------------------------------------------------

def test_host_is_free_skips_on_battery(monkeypatch):
    monkeypatch.setattr("train.hostsignals.on_battery", lambda: True)
    monkeypatch.setattr("train.hostsignals.user_idle_seconds", lambda: 9999)
    free, why = nightly.host_is_free()
    assert not free and "battery" in why


def test_host_is_free_waits_for_an_idle_machine(monkeypatch):
    monkeypatch.setattr("train.hostsignals.on_battery", lambda: False)
    monkeypatch.setattr("train.hostsignals.user_idle_seconds", lambda: 12)
    free, why = nightly.host_is_free()
    assert not free and "using this machine" in why


def test_unknown_signals_count_as_free(monkeypatch):
    """A machine that cannot report its state must not block the whole feature."""
    monkeypatch.setattr("train.hostsignals.on_battery", lambda: None)
    monkeypatch.setattr("train.hostsignals.user_idle_seconds", lambda: None)
    free, _ = nightly.host_is_free()
    assert free


def test_hostsignals_summary_never_raises_on_this_machine():
    """Whatever platform CI runs on, this must return a dict rather than explode."""
    from train.hostsignals import summary

    report = summary()
    assert set(report) == {"on_battery", "idle_seconds", "power", "activity"}
    assert report["power"] in ("on battery", "on mains", "unknown")
