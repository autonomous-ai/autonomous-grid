from __future__ import annotations

import json

import pytest

import cli
from shared.allocator.scenario import ScenarioConfig, run_scenario


@pytest.fixture(scope="module")
def representative_report():
    return run_scenario(
        ScenarioConfig(machines=8, models=8, users=36, minutes=18, seed=73)
    )


def test_scenario_exercises_heterogeneous_fleet_catalog_users_and_events(
    representative_report,
):
    report = representative_report

    assert len(report.machines) == 8
    assert len(report.models) == 8
    assert sum(row["users"] for row in report.users) == 36
    assert len({tuple(row["runtimes"]) for row in report.machines}) >= 3
    assert {"llama.cpp", "vllm", "comfyui"} <= {
        runtime for row in report.machines for runtime in row["runtimes"]
    }
    assert {"coding", "research", "image", "video"} <= {
        workload for row in report.models for workload in row["workload_scores"]
    }
    phases = {row["phase"] for row in report.timeline}
    assert {
        "warmup",
        "coding-surge",
        "creative-campaign",
        "node-outage",
        "research-recovery",
        "cooldown",
    } <= phases
    assert any(row["node_changes"] for row in report.timeline)
    assert report.metrics["loads"] > 0
    assert report.metrics["total_requests"] > 0
    assert report.metrics["direct_named_requests"] > 0
    assert report.metrics["joint_portfolio_ticks"] > 0
    assert report.metrics["portfolio_changes"] > 0
    assert any(row.get("portfolio_selection") for row in report.timeline)


def test_scenario_is_deterministic_and_json_serializable(representative_report):
    config = ScenarioConfig(machines=8, models=8, users=36, minutes=18, seed=73)
    repeated = run_scenario(config)

    assert repeated == representative_report
    encoded = json.dumps(repeated.to_dict(), sort_keys=True)
    assert "Debug this Python API" not in encoded
    assert "user-00001" not in encoded


def test_capacity_sweep_keeps_the_same_seeded_users_and_requests():
    scarce = run_scenario(
        ScenarioConfig(machines=4, models=8, users=18, minutes=12, seed=101)
    )
    roomy = run_scenario(
        ScenarioConfig(machines=12, models=8, users=18, minutes=12, seed=101)
    )

    assert [
        (row["role"], row["workload"], row["users"], row["requests"])
        for row in scarce.users
    ] == [
        (row["role"], row["workload"], row["users"], row["requests"])
        for row in roomy.users
    ]
    assert scarce.metrics["total_requests"] == roomy.metrics["total_requests"]
    assert {
        workload: row["requests"]
        for workload, row in scarce.metrics["per_workload"].items()
    } == {
        workload: row["requests"]
        for workload, row in roomy.metrics["per_workload"].items()
    }
    assert roomy.metrics["service_rate_pct"] > scarce.metrics["service_rate_pct"]
    assert (
        roomy.metrics["minimum_workload_service_pct"]
        > scarce.metrics["minimum_workload_service_pct"]
    )
    assert (
        roomy.metrics["unsatisfied_replica_minutes"]
        < scarce.metrics["unsatisfied_replica_minutes"]
    )


def test_production_controller_admits_persistent_new_media_workload():
    report = run_scenario(
        ScenarioConfig(machines=8, models=8, users=50, minutes=30, seed=42)
    )

    video = report.metrics["per_workload"]["video"]
    assert video["requests"] > 0
    assert video["service_rate_pct"] > 50
    assert report.metrics["service_rate_pct"] > 95
    assert report.metrics["unsatisfied_replica_minutes"] < 10


def test_scenario_checks_real_planner_safety_and_persistent_disk(representative_report):
    report = representative_report

    assert report.safety["passed"] is True
    assert report.safety["violations"] == ()
    assert report.metrics["minimum_remaining_disk_mb"] >= 0
    assert report.metrics["artifact_download_mb"] >= 0
    assert 0 <= report.metrics["service_rate_pct"] <= 100
    assert 0 <= report.metrics["minimum_user_service_pct"] <= 100
    assert 0 <= report.metrics["user_slo_attainment_pct"] <= 100
    assert 0 <= report.metrics["workload_slo_attainment_pct"] <= 100
    assert 0 <= report.metrics["minimum_workload_service_pct"] <= 100
    assert 0 <= report.metrics["portfolio_suitability_pct"] <= 100
    assert all(0 <= row["service_rate_pct"] <= 100 for row in report.users)
    assert "modeled_compute_cost" not in report.metrics
    assert not any("fairness" in key for key in report.metrics)


def test_scarce_fleet_reports_capacity_shortfall_instead_of_overcommit():
    report = run_scenario(
        ScenarioConfig(machines=2, models=8, users=27, minutes=12, seed=9)
    )

    assert report.safety["passed"] is True
    assert report.metrics["unsatisfied_replica_minutes"] > 0
    assert report.metrics["shortfall_by_model"]
    assert report.metrics["peak_shortfall_by_model"]
    assert report.metrics["service_rate_pct"] < 100


@pytest.mark.parametrize(
    "kwargs",
    [
        {"machines": 0},
        {"models": 33},
        {"users": 10_001},
        {"minutes": 5},
        {"minutes": 1_441},
        {"seed": True},
    ],
)
def test_scenario_configuration_is_bounded(kwargs):
    with pytest.raises(ValueError):
        ScenarioConfig(**kwargs)


def test_cli_parser_exposes_scenario_scale_duration_and_seed():
    args = cli.build_parser().parse_args(
        [
            "test",
            "scenario",
            "--machines",
            "12",
            "--models",
            "9",
            "--users",
            "500",
            "--duration",
            "2h",
            "--seed",
            "123",
            "--timeline",
            "--json",
        ]
    )

    assert args.handler is cli.cmd_test_scenario
    assert args.machines == 12
    assert args.models == 9
    assert args.users == 500
    assert args.duration == 120
    assert args.seed == 123
    assert args.timeline is True
    assert args.json is True


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--machines", "65"),
        ("--models", "0"),
        ("--users", "10001"),
        ("--duration", "5m"),
        ("--duration", "tomorrow"),
    ],
)
def test_cli_parser_rejects_invalid_scenario_bounds(flag, value):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["test", "scenario", flag, value])


def test_scenario_cli_prints_human_scorecard(capsys):
    args = cli.build_parser().parse_args(
        [
            "test",
            "scenario",
            "--machines",
            "6",
            "--models",
            "6",
            "--users",
            "18",
            "--duration",
            "6m",
            "--seed",
            "7",
        ]
    )

    assert args.handler(args) == 0
    output = capsys.readouterr().out
    assert "Planning simulation only" in output
    assert "Heterogeneous logical fleet" in output
    assert "Model portfolio" in output
    assert "User population" in output
    assert "Allocation timeline" in output
    assert "Scorecard" in output
    assert "least-served user" in output
    assert "joint portfolio optimizer" in output
    assert "portfolio " in output
    assert "fairness" not in output
    assert "compute cost" not in output
    assert "Safety: PASS" in output


def test_scenario_cli_json_is_machine_readable(capsys):
    args = cli.build_parser().parse_args(
        [
            "test",
            "scenario",
            "--machines",
            "4",
            "--models",
            "4",
            "--users",
            "9",
            "--duration",
            "6m",
            "--seed",
            "11",
            "--json",
        ]
    )

    assert args.handler(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["configuration"]["mode"] == "deterministic planning simulation"
    assert payload["metrics"]["total_requests"] >= 0
    assert payload["safety"]["passed"] is True
