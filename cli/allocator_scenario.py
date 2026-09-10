"""Operator-facing heterogeneous allocator scenario lab."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from typing import Any

from shared.allocator.graduation import (
    GraduationConfig,
    GraduationReport,
    run_graduation,
)
from shared.allocator.scenario import (
    SCENARIO_WORKLOADS,
    ScenarioConfig,
    ScenarioReport,
    run_scenario,
)


def bounded_scenario_machines(value: str) -> int:
    return _bounded_int(value, name="machines", minimum=1, maximum=64)


def bounded_scenario_models(value: str) -> int:
    return _bounded_int(value, name="models", minimum=1, maximum=32)


def bounded_scenario_users(value: str) -> int:
    return _bounded_int(value, name="users", minimum=1, maximum=10_000)


def simulated_minutes(value: str) -> int:
    match = re.fullmatch(r"([0-9]+)([mh]?)", value.strip().lower())
    if not match:
        raise argparse.ArgumentTypeError("duration must be minutes (30 or 30m) or hours (2h)")
    amount = int(match.group(1))
    minutes = amount * 60 if match.group(2) == "h" else amount
    if not 6 <= minutes <= 1_440:
        raise argparse.ArgumentTypeError("duration must be between 6 minutes and 24 hours")
    return minutes


def workload_trace_binding(value: str) -> tuple[str, str]:
    workload, separator, path = value.partition("=")
    workload = workload.strip()
    path = path.strip()
    if not separator or not workload or not path:
        raise argparse.ArgumentTypeError(
            "workload trace must be WORKLOAD=CSV_PATH, for example coding=trace.csv"
        )
    if workload not in SCENARIO_WORKLOADS:
        raise argparse.ArgumentTypeError(
            f"unknown workload {workload!r}; choose from " + ", ".join(SCENARIO_WORKLOADS)
        )
    return workload, path


def graduation_machine_counts(value: str) -> tuple[int, ...]:
    try:
        result = tuple(bounded_scenario_machines(item) for item in value.split(","))
    except argparse.ArgumentTypeError:
        raise
    if not result or len(set(result)) != len(result):
        raise argparse.ArgumentTypeError("machine counts must be a unique comma-separated list")
    return result


def graduation_seeds(value: str) -> tuple[int, ...]:
    try:
        result = tuple(int(item) for item in value.split(","))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("seeds must be comma-separated integers") from exc
    if not result or len(set(result)) != len(result):
        raise argparse.ArgumentTypeError("seeds must be a unique comma-separated list")
    return result


def cmd_test_scenario(args: argparse.Namespace) -> int:
    config = ScenarioConfig(
        machines=args.machines,
        models=args.models,
        users=args.users,
        minutes=args.duration,
        seed=args.seed,
        workload_traces=tuple(args.workload_trace),
        oracle=args.oracle,
        strategy=args.strategy,
    )
    report = run_scenario(config)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_report(report, full_timeline=args.timeline)
    return 0 if report.safety["passed"] else 1


def cmd_test_graduate(args: argparse.Namespace) -> int:
    report = run_graduation(
        GraduationConfig(
            machine_counts=args.machines,
            seeds=args.seeds,
            models=args.models,
            users=args.users,
            minutes=args.duration,
        )
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        _print_graduation(report)
    return 0 if report.passed else 1


def _print_graduation(report: GraduationReport) -> None:
    cfg = report.configuration
    print(
        "Allocator graduation · machines "
        + ",".join(str(item) for item in cfg["machine_counts"])
        + " · seeds "
        + ",".join(str(item) for item in cfg["seeds"])
        + f" · {cfg['minutes']} minutes per run"
    )
    print(
        "Identical traces compare smart allocation with reactive-only, current-demand greedy, "
        "and fixed static controls.\n"
    )
    print("Case comparison")
    for row in report.comparisons:
        print(
            f"  {row['machines']:>2} nodes · seed {row['seed']:<6} "
            f"smart {row['smart_score']:>6.2f} · "
            f"best {row['best_baseline']:<8} {row['best_baseline_score']:>6.2f} · "
            f"delta {row['score_delta']:>+6.2f} · "
            f"service-vs-static {row['service_vs_static_delta']:>+6.2f} · "
            f"coverage-vs-static {row['coverage_vs_static_delta']:>+6.2f} · "
            f"churn {row['smart_churn']}/{row['greedy_churn']}"
        )
    print("\nGraduation gates")
    for gate in report.gates:
        print(f"  {'PASS' if gate['passed'] else 'FAIL'} {gate['name']}: {gate['detail']}")
    print(
        f"\nAllocator graduation: {'PASS' if report.passed else 'NOT YET'} "
        f"({report.elapsed_seconds:.1f}s wall time)"
    )


def _print_report(report: ScenarioReport, *, full_timeline: bool) -> None:
    cfg = report.configuration
    print(
        "Allocator scenario lab · "
        f"{cfg['machines']} logical machines · {cfg['models']} models · "
        f"{cfg['users']} users · {cfg['minutes']} simulated minutes · seed {cfg['seed']}"
    )
    print(f"Allocation strategy: {cfg['strategy']}")
    print("Planning simulation only: hardware telemetry is modeled; no GPU processes are started.")
    print("The real `grid test demo` remains the load/warm/drain/unload process test.\n")
    if cfg["workload_traces"]:
        bindings = ", ".join(
            f"{item['workload']}={item['path']}" for item in cfg["workload_traces"]
        )
        print(f"Demand replay: external request-rate traces for {bindings}.")
        print("Trace values change demand timing, while normalization preserves workload scale.\n")
    print("Lifecycle timing: a load requested this minute becomes ready on the following minute.\n")

    print("Heterogeneous logical fleet")
    machine_groups = Counter(
        (
            row["hardware"],
            ",".join(row["runtimes"]),
            row["memory_mb"],
            row["disk_available_mb"],
        )
        for row in report.machines
    )
    for (hardware, runtimes, memory_mb, disk_mb), count in sorted(machine_groups.items()):
        print(
            f"  {count:>2}× {hardware} · {runtimes} · "
            f"{memory_mb / 1024:.0f} GiB memory · {disk_mb / 1024:.0f} GiB disk free"
        )

    print("\nModel portfolio")
    for row in report.models:
        capabilities = ", ".join(
            f"{name}:{score:g}" for name, score in sorted(row["workload_scores"].items())
        )
        print(
            f"  {row['model_id']:<22} {row['job']:<24} "
            f"{row['memory_mb'] / 1024:>5.1f} GiB · {capabilities}"
        )

    print("\nUser population")
    print(
        "  "
        + " · ".join(
            f"{row['role']}={row['users']} ({row['service_rate_pct']:.0f}% served)"
            for row in report.users
        )
    )

    print("\nAllocation timeline")
    timeline = report.timeline if full_timeline else _notable_timeline(report.timeline)
    for row in timeline:
        changes: list[str] = []
        if row["node_changes"]:
            changes.append("nodes " + ", ".join(row["node_changes"]))
        if row.get("portfolio_changed") and row.get("portfolio_selection"):
            portfolio = ", ".join(
                f"{workload}->{model_id or 'deferred'}"
                for workload, model_id in sorted(row["portfolio_selection"].items())
            )
            changes.append("portfolio " + portfolio)
        if row.get("prefetches"):
            changes.append("prefetch " + ", ".join(row["prefetches"][:3]))
            if len(row["prefetches"]) > 3:
                changes[-1] += f" +{len(row['prefetches']) - 3}"
        if row.get("artifact_evictions"):
            replacements = [
                item for item in row["artifact_evictions"] if "->" in item
            ]
            expirations = [
                item for item in row["artifact_evictions"] if "->" not in item
            ]
            if replacements:
                changes.append(
                    "replace predictive cache " + ", ".join(replacements[:3])
                )
            if expirations:
                changes.append(
                    "expire predictive cache " + ", ".join(expirations[:3])
                )
            if len(row["artifact_evictions"]) > 3:
                changes[-1] += f" +{len(row['artifact_evictions']) - 3}"
        if row["loads"]:
            changes.append("load " + ", ".join(row["loads"][:3]))
            if len(row["loads"]) > 3:
                changes[-1] += f" +{len(row['loads']) - 3}"
        if row["unloads"]:
            changes.append("unload " + ", ".join(row["unloads"][:3]))
            if len(row["unloads"]) > 3:
                changes[-1] += f" +{len(row['unloads']) - 3}"
        if row["unsatisfied"]:
            changes.append(
                "short "
                + ", ".join(
                    f"{item['model']}:{item['missing']}" for item in row["unsatisfied"]
                )
            )
        if row.get("mutation_deferrals"):
            changes.append(
                "defer " + ", ".join(row["mutation_deferrals"][:2])
            )
        if row.get("overloaded_models"):
            pressure = [
                f"{model} {values['offered_concurrency']:g}/{values['ready_capacity']:g} q{values['queue_depth']}"
                for model, values in sorted(row["overloaded_models"].items())[:2]
            ]
            if len(row["overloaded_models"]) > 2:
                pressure.append(f"+{len(row['overloaded_models']) - 2}")
            changes.append("overload " + ", ".join(pressure))
        detail = " | ".join(changes) or "placement stable"
        print(
            f"  m{row['minute']:02d} {row['phase']:<19} "
            f"requests={row['requests']:<3} · served={row['service_rate_pct']:>6.2f}% · {detail}"
        )
        if full_timeline or row.get("portfolio_changed"):
            for admission in row.get("portfolio_admissions") or ():
                sequence_sources = admission.get("demand_correlation_sources") or ()
                if sequence_sources:
                    confidence = 100.0 * float(
                        admission.get("demand_correlation_confidence") or 0.0
                    )
                    prediction_lead = admission.get("prediction_lead_seconds")
                    lead_label = (
                        f" · expected in {float(prediction_lead):.0f}s"
                        if prediction_lead is not None
                        else ""
                    )
                    print(
                        "      proactive: learned workflow "
                        + ", ".join(str(item) for item in sequence_sources)
                        + f" → {admission.get('workload') or 'unknown'} · "
                        f"{confidence:.0f}% confidence{lead_label}"
                    )
                if admission.get("state") == "ready":
                    continue
                print(
                    f"      {admission.get('workload') or 'unknown'}: "
                    f"{admission.get('state') or 'unknown'} via "
                    f"{admission.get('model_id') or 'no-model'} · "
                    f"{admission.get('reason') or 'no admission reason'}"
                )
    if not full_timeline and len(timeline) < len(report.timeline):
        print(
            f"  … {len(report.timeline) - len(timeline)} intermediate changes hidden; "
            "add --timeline to show all"
        )

    metrics = report.metrics
    print("\nScorecard")
    print(f"  overall allocator score       {metrics['overall_score']:>7.2f}/100")
    print(f"  demand served                 {metrics['service_rate_pct']:>7.2f}%")
    print(f"  least-served user             {metrics['minimum_user_service_pct']:>7.2f}%")
    print(
        "  least-served allocatable user "
        f"{metrics['allocatable_minimum_user_service_pct']:>7.2f}%"
    )
    print(f"  users meeting 90% SLO         {metrics['user_slo_attainment_pct']:>7.2f}%")
    print(f"  workloads meeting 90% SLO     {metrics['workload_slo_attainment_pct']:>7.2f}%")
    print(f"  portfolio suitability         {metrics['portfolio_suitability_pct']:>7.2f}%")
    print(
        f"  feasible-workload coverage    {metrics['workload_coverage_utility_pct']:>7.2f}%"
    )
    if metrics["structurally_infeasible_workloads"]:
        print(
            "  structurally infeasible       "
            + ", ".join(metrics["structurally_infeasible_workloads"])
        )
    print(
        f"  memory utilization        avg {metrics['average_memory_utilization_pct']:>6.2f}% · "
        f"peak {metrics['peak_memory_utilization_pct']:.2f}%"
    )
    print(
        f"  lifecycle changes          load {metrics['loads']} · unload {metrics['unloads']} · "
        f"migrate {metrics['migrations']} · cache hits {metrics['cache_hit_rate_pct']:.2f}%"
    )
    print(
        f"  artifact downloads            {metrics['artifact_download_mb'] / 1024:.1f} GiB · "
        f"minimum disk remaining {metrics['minimum_remaining_disk_mb'] / 1024:.1f} GiB"
    )
    print(
        f"  predictive cache          prefetch {metrics['predictive_prefetches']} · "
        f"hits {metrics['predictive_prefetch_hits']} "
        f"({metrics['predictive_prefetch_hit_rate_pct']:.2f}%) · "
        f"unused {metrics['unused_predictive_prefetches']} · "
        f"evicted {metrics['predictive_prefetch_evictions']} "
        f"({metrics['predictive_prefetch_replacements']} replacements) "
        f"({metrics['predictive_prefetch_reclaimed_mb'] / 1024:.1f} GiB reclaimed) · "
        f"saved {metrics['predictive_cold_start_seconds_avoided']:.1f}s startup · "
        f"avg lead {metrics['average_predictive_prefetch_lead_minutes']:.1f}m"
    )
    print(
        f"  capacity shortfall            {metrics['unsatisfied_replica_minutes']} "
        "replica-minutes"
    )
    print(
        f"  realized serving pressure     {metrics['overloaded_model_minutes']} "
        f"overloaded model-minutes · peak queue {metrics['peak_modeled_queue_depth']} · "
        f"failed observations {metrics['realized_failure_observations']}"
    )
    if metrics["shortfall_by_model"]:
        largest_shortfall = max(
            metrics["shortfall_by_model"],
            key=lambda model: (
                metrics["shortfall_by_model"][model],
                model,
            ),
        )
        profile = next(row for row in report.models if row["model_id"] == largest_shortfall)
        runtimes = "/".join(profile["runtimes"])
        print(
            f"  capacity recommendation       add up to "
            f"{metrics['peak_shortfall_by_model'][largest_shortfall]} {runtimes} slot(s) "
            f"for {largest_shortfall}"
        )
    print(f"  catalog gaps                  {metrics['catalog_gap_requests']} requests")
    print(f"  direct named-model traffic    {metrics['direct_named_requests']} requests")
    print(
        f"  joint portfolio optimizer     {metrics['joint_portfolio_ticks']} ticks · "
        f"{metrics['portfolio_changes']} selection changes"
    )
    admission_states = metrics.get("admission_state_minutes") or {}
    if admission_states:
        print(
            "  workload admission time       "
            + " · ".join(
                f"{state} {minutes}m"
                for state, minutes in sorted(admission_states.items())
            )
        )

    weakest = sorted(
        metrics["per_workload"].items(),
        key=lambda item: (item[1]["service_rate_pct"], item[0]),
    )[:3]
    if weakest:
        print(
            "  weakest workloads             "
            + " · ".join(
                f"{name} {row['service_rate_pct']:.1f}%" for name, row in weakest
            )
        )

    if report.safety["passed"]:
        print("\nSafety: PASS · no overcommit, incompatible placement, or lifecycle violation detected")
    else:
        print(f"\nSafety: FAIL · {len(report.safety['violations'])} violation(s)")
        for violation in report.safety["violations"][:10]:
            print(f"  {violation}")
    oracle = metrics.get("oracle")
    if oracle:
        print("\nClairvoyant small-fleet benchmark")
        print(
            f"  service ceiling              {oracle['service_ceiling_pct']:>7.2f}% · "
            f"potential gain {oracle['potential_gain_pct_points']:.2f} points"
        )
        print(
            f"  exhaustive search             {oracle['states_evaluated']} placements · "
            f"{oracle['mutations']} lifecycle mutations"
        )
        print(
            "  evidence                      "
            + str(oracle["interpretation"])
        )
        if not oracle["artifact_feasible"]:
            overage = ", ".join(
                f"{node} +{amount / 1024:.1f} GiB"
                for node, amount in oracle["artifact_overage_mb"].items()
            )
            print(f"  cache preparation needed      {overage}")
    print("JSON report: rerun with --json. Repeatability: use the same --seed.")


def _notable_timeline(rows: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    if len(rows) <= 12:
        return rows
    selected: list[dict[str, Any]] = []
    seen_phases: set[str] = set()
    for row in rows:
        if (
            row["phase"] not in seen_phases
            or row["node_changes"]
            or row["loads"]
            or row["unloads"]
            or row.get("prefetches")
            or row.get("artifact_evictions")
            or row.get("overloaded_models")
        ):
            selected.append(row)
            seen_phases.add(row["phase"])
    if len(selected) > 12:
        selected = selected[:11] + [selected[-1]]
    return tuple(selected)


def _bounded_int(value: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
    if not minimum <= result <= maximum:
        raise argparse.ArgumentTypeError(f"{name} must be in [{minimum}, {maximum}]")
    return result
