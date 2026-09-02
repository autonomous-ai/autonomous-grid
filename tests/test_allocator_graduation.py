from __future__ import annotations

from types import SimpleNamespace

import cli
from shared.allocator import graduation as graduation_module
from shared.allocator.graduation import (
    GraduationConfig,
    GraduationReport,
    run_graduation,
)


def _fake_scenario(config):
    rows = {
        "smart": (90.0, 92.0, 80.0, 4, 10.0),
        "reactive": (87.0, 88.0, 72.0, 8, 0.0),
        "greedy": (85.0, 87.0, 70.0, 10, 0.0),
        "static": (80.0, 82.0, 68.0, 2, 0.0),
    }
    score, service, minimum_workload, churn, avoided = rows[config.strategy]
    return SimpleNamespace(
        safety={"passed": True},
        metrics={
            "total_requests": 100,
            "overall_score": score,
            "service_rate_pct": service,
            "minimum_user_service_pct": minimum_workload,
            "minimum_workload_service_pct": minimum_workload,
            "portfolio_suitability_pct": 90.0,
            "workload_coverage_utility_pct": (
                90.0 if config.strategy == "smart" else 80.0
            ),
            "loads": churn,
            "unloads": 0,
            "rapid_lifecycle_reversals": 0,
            "lifecycle_changes_per_node_hour": 1.0,
            "modeled_cold_start_seconds": 20.0,
            "predictive_cold_start_seconds_avoided": avoided,
        },
    )


def test_graduation_compares_identical_cases_and_applies_explicit_gates(monkeypatch):
    monkeypatch.setattr(graduation_module, "run_scenario", _fake_scenario)

    report = run_graduation(
        GraduationConfig(machine_counts=(2, 4, 8), seeds=(7,), minutes=6)
    )

    assert report.passed is True
    assert len(report.runs) == 12
    assert len(report.comparisons) == 3
    assert all(row["best_baseline"] == "reactive" for row in report.comparisons)
    assert all(gate["passed"] for gate in report.gates)
    assert report.to_dict()["configuration"]["machine_counts"] == (2, 4, 8)


def test_graduation_fails_when_smart_policy_regresses(monkeypatch):
    def regressed(config):
        report = _fake_scenario(config)
        if config.strategy == "smart":
            report.metrics["overall_score"] = 60.0
            report.metrics["service_rate_pct"] = 60.0
        return report

    monkeypatch.setattr(graduation_module, "run_scenario", regressed)

    report = run_graduation(
        GraduationConfig(machine_counts=(4,), seeds=(7,), minutes=6)
    )

    assert report.passed is False
    failed = {gate["name"] for gate in report.gates if not gate["passed"]}
    assert {"baseline_competitiveness", "adaptive_service"} <= failed


def test_graduation_cli_parser_exposes_matrix_controls():
    args = cli.build_parser().parse_args(
        [
            "test",
            "graduate",
            "--machines",
            "2,4,8",
            "--seeds",
            "7,91",
            "--models",
            "9",
            "--users",
            "100",
            "--duration",
            "2h",
            "--json",
        ]
    )

    assert args.handler is cli.cmd_test_graduate
    assert args.machines == (2, 4, 8)
    assert args.seeds == (7, 91)
    assert args.models == 9
    assert args.users == 100
    assert args.duration == 120
    assert args.json is True


def test_graduation_cli_prints_actionable_gate_results(monkeypatch, capsys):
    report = GraduationReport(
        configuration={"machine_counts": (2,), "seeds": (7,), "minutes": 6},
        runs=(),
        comparisons=(
            {
                "machines": 2,
                "seed": 7,
                "smart_score": 80.0,
                "best_baseline": "static",
                "best_baseline_score": 82.0,
                "score_delta": -2.0,
                "service_vs_static_delta": 4.0,
                "coverage_vs_static_delta": 5.0,
                "smart_churn": 3,
                "greedy_churn": 8,
            },
        ),
        gates=(
            {"name": "safety", "passed": True, "detail": "zero violations"},
            {"name": "quality", "passed": False, "detail": "score regressed"},
        ),
        passed=False,
        elapsed_seconds=1.5,
    )
    monkeypatch.setattr("cli.allocator_scenario.run_graduation", lambda _config: report)
    args = cli.build_parser().parse_args(
        ["test", "graduate", "--machines", "2", "--seeds", "7", "--duration", "6m"]
    )

    assert args.handler(args) == 1
    output = capsys.readouterr().out
    assert "Case comparison" in output
    assert "PASS safety" in output
    assert "FAIL quality" in output
    assert "Allocator graduation: NOT YET" in output
