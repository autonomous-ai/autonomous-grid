"""Repeatable allocator graduation matrix against simpler control policies."""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Any

from shared.allocator.scenario import ScenarioConfig, run_scenario

BASELINE_STRATEGIES = ("reactive", "greedy", "static")
ALL_STRATEGIES = ("smart", *BASELINE_STRATEGIES)


@dataclass(frozen=True, slots=True)
class GraduationConfig:
    machine_counts: tuple[int, ...] = (2, 4, 8)
    seeds: tuple[int, ...] = (42, 144)
    models: int = 8
    users: int = 50
    minutes: int = 120
    score_regression_tolerance: float = 2.0
    service_regression_tolerance: float = 1.0
    minimum_adaptive_service_gain: float = 3.0
    maximum_lifecycle_changes_per_node_hour: float = 9.0
    maximum_rapid_reversal_fraction: float = 0.20

    def __post_init__(self) -> None:
        if (
            not self.machine_counts
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 64
                for value in self.machine_counts
            )
            or len(set(self.machine_counts)) != len(self.machine_counts)
        ):
            raise ValueError("machine_counts must contain unique integers in [1, 64]")
        if (
            not self.seeds
            or any(isinstance(value, bool) or not isinstance(value, int) for value in self.seeds)
            or len(set(self.seeds)) != len(self.seeds)
        ):
            raise ValueError("seeds must contain unique integers")
        # Reuse scenario validation for the shared scale bounds.
        ScenarioConfig(
            machines=self.machine_counts[0],
            models=self.models,
            users=self.users,
            minutes=self.minutes,
            seed=self.seeds[0],
        )
        for name in (
            "score_regression_tolerance",
            "service_regression_tolerance",
            "minimum_adaptive_service_gain",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if (
            not math.isfinite(self.maximum_lifecycle_changes_per_node_hour)
            or self.maximum_lifecycle_changes_per_node_hour <= 0
        ):
            raise ValueError(
                "maximum_lifecycle_changes_per_node_hour must be finite and positive"
            )
        if (
            not math.isfinite(self.maximum_rapid_reversal_fraction)
            or not 0 <= self.maximum_rapid_reversal_fraction <= 1
        ):
            raise ValueError("maximum_rapid_reversal_fraction must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class GraduationReport:
    configuration: dict[str, Any]
    runs: tuple[dict[str, Any], ...]
    comparisons: tuple[dict[str, Any], ...]
    gates: tuple[dict[str, Any], ...]
    passed: bool
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_graduation(config: GraduationConfig) -> GraduationReport:
    """Run every policy on identical traces and evaluate explicit graduation gates."""

    started = time.perf_counter()
    runs: list[dict[str, Any]] = []
    by_case: dict[tuple[int, int], dict[str, dict[str, Any]]] = {}
    for machines in config.machine_counts:
        for seed in config.seeds:
            case: dict[str, dict[str, Any]] = {}
            expected_requests: int | None = None
            for strategy in ALL_STRATEGIES:
                run_started = time.perf_counter()
                report = run_scenario(
                    ScenarioConfig(
                        machines=machines,
                        models=config.models,
                        users=config.users,
                        minutes=config.minutes,
                        seed=seed,
                        strategy=strategy,
                    )
                )
                elapsed = time.perf_counter() - run_started
                metrics = report.metrics
                requests = int(metrics["total_requests"])
                if expected_requests is None:
                    expected_requests = requests
                demand_identical = requests == expected_requests
                row = {
                    "machines": machines,
                    "seed": seed,
                    "strategy": strategy,
                    "safety_passed": bool(report.safety["passed"]),
                    "demand_identical": demand_identical,
                    "overall_score": float(metrics["overall_score"]),
                    "service_rate_pct": float(metrics["service_rate_pct"]),
                    "minimum_user_service_pct": float(
                        metrics["minimum_user_service_pct"]
                    ),
                    "minimum_workload_service_pct": float(
                        metrics["minimum_workload_service_pct"]
                    ),
                    "portfolio_suitability_pct": float(
                        metrics["portfolio_suitability_pct"]
                    ),
                    "workload_coverage_utility_pct": float(
                        metrics["workload_coverage_utility_pct"]
                    ),
                    "lifecycle_changes": int(metrics["loads"])
                    + int(metrics["unloads"]),
                    "rapid_lifecycle_reversals": int(
                        metrics["rapid_lifecycle_reversals"]
                    ),
                    "lifecycle_changes_per_node_hour": float(
                        metrics["lifecycle_changes_per_node_hour"]
                    ),
                    "modeled_cold_start_seconds": float(
                        metrics["modeled_cold_start_seconds"]
                    ),
                    "predictive_cold_start_seconds_avoided": float(
                        metrics["predictive_cold_start_seconds_avoided"]
                    ),
                    "elapsed_seconds": round(elapsed, 6),
                }
                runs.append(row)
                case[strategy] = row
            by_case[(machines, seed)] = case

    comparisons = []
    for (machines, seed), case in sorted(by_case.items()):
        smart = case["smart"]
        best_baseline = max(
            (case[strategy] for strategy in BASELINE_STRATEGIES),
            key=lambda row: (row["overall_score"], row["strategy"]),
        )
        comparisons.append(
            {
                "machines": machines,
                "seed": seed,
                "best_baseline": best_baseline["strategy"],
                "smart_score": smart["overall_score"],
                "best_baseline_score": best_baseline["overall_score"],
                "score_delta": round(
                    smart["overall_score"] - best_baseline["overall_score"], 2
                ),
                "service_vs_static_delta": round(
                    smart["service_rate_pct"] - case["static"]["service_rate_pct"],
                    2,
                ),
                "minimum_workload_vs_greedy_delta": round(
                    smart["minimum_workload_service_pct"]
                    - case["greedy"]["minimum_workload_service_pct"],
                    2,
                ),
                "coverage_vs_static_delta": round(
                    smart["workload_coverage_utility_pct"]
                    - case["static"]["workload_coverage_utility_pct"],
                    2,
                ),
                "smart_churn": smart["lifecycle_changes"],
                "greedy_churn": case["greedy"]["lifecycle_changes"],
                "proactive_startup_seconds_avoided": smart[
                    "predictive_cold_start_seconds_avoided"
                ],
            }
        )

    smart_rows = [row for row in runs if row["strategy"] == "smart"]
    reactive_rows = [row for row in runs if row["strategy"] == "reactive"]
    adaptive_gains = [row["service_vs_static_delta"] for row in comparisons]
    score_deltas = [row["score_delta"] for row in comparisons]
    smart_churn = sum(row["lifecycle_changes"] for row in smart_rows)
    greedy_churn = sum(
        row["lifecycle_changes"] for row in runs if row["strategy"] == "greedy"
    )
    smart_reversals = sum(row["rapid_lifecycle_reversals"] for row in smart_rows)
    peak_churn_rate = max(
        (row["lifecycle_changes_per_node_hour"] for row in smart_rows),
        default=0.0,
    )
    largest_fleet = max(config.machine_counts)
    coverage_gains = [row["coverage_vs_static_delta"] for row in comparisons]
    adaptive_tradeoffs = [
        row["service_vs_static_delta"] + row["coverage_vs_static_delta"]
        for row in comparisons
    ]
    largest_balanced_deltas = [
        row["minimum_workload_vs_greedy_delta"]
        for row in comparisons
        if row["machines"] == largest_fleet
    ]
    proactive_savings = sum(
        row["predictive_cold_start_seconds_avoided"] for row in smart_rows
    )
    reactive_savings = sum(
        row["predictive_cold_start_seconds_avoided"] for row in reactive_rows
    )

    gates = (
        _gate(
            "safety",
            all(row["safety_passed"] for row in smart_rows),
            "zero smart-policy safety violations across the matrix",
        ),
        _gate(
            "trace_parity",
            all(row["demand_identical"] for row in runs),
            "every strategy received the same request count per case",
        ),
        _gate(
            "baseline_competitiveness",
            min(score_deltas, default=0.0) >= -config.score_regression_tolerance,
            f"worst smart score delta {min(score_deltas, default=0.0):.2f} points "
            f"(floor {-config.score_regression_tolerance:.2f})",
        ),
        _gate(
            "adaptive_service",
            min(adaptive_tradeoffs, default=0.0)
            >= -config.service_regression_tolerance
            and statistics.median(adaptive_gains or [0.0])
            >= config.minimum_adaptive_service_gain,
            f"median service gain over static {statistics.median(adaptive_gains or [0.0]):.2f} "
            f"points; worst service+coverage tradeoff "
            f"{min(adaptive_tradeoffs, default=0.0):.2f}",
        ),
        _gate(
            "churn_efficiency",
            peak_churn_rate <= config.maximum_lifecycle_changes_per_node_hour
            and smart_reversals
            <= config.maximum_rapid_reversal_fraction * max(1, smart_churn),
            f"peak {peak_churn_rate:.2f} changes/node-hour "
            f"(limit {config.maximum_lifecycle_changes_per_node_hour:.2f}); "
            f"rapid reversals {smart_reversals}/{smart_churn} "
            f"(limit {config.maximum_rapid_reversal_fraction:.0%}); "
            f"greedy reference {greedy_churn} changes",
        ),
        _gate(
            "balanced_roomy_service",
            min(largest_balanced_deltas, default=0.0) >= -2.0,
            "worst minimum-workload delta versus greedy on the largest fleet "
            f"{min(largest_balanced_deltas, default=0.0):.2f} points",
        ),
        _gate(
            "feasible_workload_coverage",
            min(coverage_gains, default=0.0) >= -2.0
            and statistics.median(coverage_gains or [0.0]) >= 2.0,
            f"median feasible-workload coverage gain over static "
            f"{statistics.median(coverage_gains or [0.0]):.2f} points; "
            f"worst {min(coverage_gains, default=0.0):.2f}",
        ),
        _gate(
            "proactive_value",
            proactive_savings > reactive_savings,
            f"smart/reactive avoided startup {proactive_savings:.1f}s/"
            f"{reactive_savings:.1f}s",
        ),
    )
    return GraduationReport(
        configuration=asdict(config),
        runs=tuple(runs),
        comparisons=tuple(comparisons),
        gates=gates,
        passed=all(gate["passed"] for gate in gates),
        elapsed_seconds=round(time.perf_counter() - started, 6),
    )


def _gate(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}
