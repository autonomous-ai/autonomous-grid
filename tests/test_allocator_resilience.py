from __future__ import annotations

import json
import stat

import pytest

from cli.allocator_resilience import cmd_allocator_resilience, resilience_hours
from cli.parser import build_parser
from shared.allocator.resilience import ResilienceConfig, run_resilience_soak


def compact_config(**overrides) -> ResilienceConfig:
    values = {
        "hours": 1,
        "interval_seconds": 300,
        "node_partition_every": 3,
        "relay_outage_every": 4,
        "controller_failover_every": 5,
    }
    values.update(overrides)
    return ResilienceConfig(**values)


def test_resilience_soak_exercises_every_fault_and_lifecycle(tmp_path):
    report = run_resilience_soak(compact_config(), tmp_path / "run")

    assert report.passed
    assert report.completed_cycles == 12
    assert {event["kind"] for event in report.events} == {
        "node_partition",
        "relay_outage",
        "controller_failover",
    }
    assert report.final_controller_term >= 3
    assert report.checks["old_leader_fenced"] >= 2
    assert report.checks["relay_outage_preserved_state"] >= 2
    assert report.checks["no_partitioned_assignment"] > 0
    assert report.checks["no_memory_overcommit"] > 0
    assert report.checks["action_warm"] > 0
    assert report.checks["action_drain"] > 0
    assert report.checks["action_unload"] > 0
    assert stat.S_IMODE((tmp_path / "run" / "report.json").stat().st_mode) == 0o600


def test_resilience_resume_continues_a_truthful_partial_checkpoint(tmp_path, monkeypatch):
    root = tmp_path / "run"
    config = compact_config()
    module = __import__("shared.allocator.resilience", fromlist=["jsonio"])
    original = module.jsonio.atomic_write_json
    writes = 0

    def interrupt_after_four(path, value, **kwargs):
        nonlocal writes
        result = original(path, value, **kwargs)
        if path.name == "checkpoint.json":
            writes += 1
            if writes == 4:
                raise KeyboardInterrupt
        return result

    monkeypatch.setattr(
        "shared.allocator.resilience.jsonio.atomic_write_json", interrupt_after_four
    )
    with pytest.raises(KeyboardInterrupt):
        run_resilience_soak(config, root)
    checkpoint = json.loads((root / "checkpoint.json").read_text())
    assert checkpoint["completed_cycles"] == 4

    monkeypatch.setattr(
        "shared.allocator.resilience.jsonio.atomic_write_json", original
    )
    resumed = run_resilience_soak(config, root, resume=True)
    assert resumed.passed
    assert resumed.completed_cycles == config.cycles


def test_resume_rejects_configuration_drift(tmp_path):
    root = tmp_path / "run"
    run_resilience_soak(compact_config(), root)
    with pytest.raises(ValueError, match="configuration does not match"):
        run_resilience_soak(compact_config(seed=99), root, resume=True)


def test_relay_outage_does_not_turn_into_an_empty_fleet_reconciliation(tmp_path):
    report = run_resilience_soak(
        compact_config(
            node_partition_every=0,
            relay_outage_every=2,
            controller_failover_every=0,
        ),
        tmp_path / "run",
    )
    assert report.passed
    assert report.checks["relay_outage_preserved_state"] == 5
    assert not report.failures


def test_parser_exposes_accelerated_and_wall_clock_resilience_modes():
    parser = build_parser()
    args = parser.parse_args(
        [
            "allocator",
            "resilience",
            "--duration",
            "3d",
            "--interval",
            "600",
            "--wall-clock",
            "--resume",
        ]
    )
    assert args.duration == 72
    assert args.interval == 600
    assert args.wall_clock is True
    assert args.resume is True
    assert args.handler is cmd_allocator_resilience


@pytest.mark.parametrize("value", ["0h", "31d", "wat", "-1"])
def test_resilience_duration_rejects_unsafe_bounds(value):
    with pytest.raises(Exception):
        resilience_hours(value)


def test_json_cli_is_machine_readable(tmp_path, capsys):
    parser = build_parser()
    args = parser.parse_args(
        [
            "allocator",
            "resilience",
            "--duration",
            "0.1h",
            "--interval",
            "300",
            "--state-dir",
            str(tmp_path / "cli"),
            "--json",
        ]
    )
    assert args.handler(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
    assert payload["completed_cycles"] == 2
