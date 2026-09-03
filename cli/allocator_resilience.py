"""Operator CLI for allocator resilience and soak qualification."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from shared import paths
from shared.allocator.resilience import ResilienceConfig, run_resilience_soak


def resilience_hours(value: str) -> float:
    raw = value.strip().lower()
    multiplier = 1.0
    if raw.endswith("d"):
        multiplier = 24.0
        raw = raw[:-1]
    elif raw.endswith("h"):
        raw = raw[:-1]
    try:
        hours = float(raw) * multiplier
        ResilienceConfig(hours=hours)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("duration must be between 0 and 30d, e.g. 72h or 3d") from exc
    return hours


def positive_interval(value: str) -> float:
    try:
        interval = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("interval must be a number of seconds") from None
    if interval <= 0:
        raise argparse.ArgumentTypeError("interval must be positive")
    return interval


def nonnegative_frequency(value: str) -> int:
    try:
        frequency = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("fault frequency must be a whole number of cycles") from None
    if frequency < 0:
        raise argparse.ArgumentTypeError("fault frequency cannot be negative")
    return frequency


def cmd_allocator_resilience(args: argparse.Namespace) -> int:
    config = ResilienceConfig(
        hours=args.duration,
        interval_seconds=args.interval,
        accelerated=not args.wall_clock,
        seed=args.seed,
        node_partition_every=args.node_partition_every,
        relay_outage_every=args.relay_outage_every,
        controller_failover_every=args.controller_failover_every,
    )
    root = Path(args.state_dir).expanduser() if args.state_dir else _default_root()

    def progress(row: dict[str, object]) -> None:
        if args.json or args.quiet:
            return
        cycle = int(row["cycle"])
        cycles = int(row["cycles"])
        event = str(row["event"])
        if event != "steady" or cycle == 1 or cycle == cycles or cycle % max(1, cycles // 20) == 0:
            print(
                f"[{cycle:>5}/{cycles}] {event:<20} "
                f"term={row['term']} failures={row['failures']}"
            )

    if not args.json:
        clock = "wall-clock" if args.wall_clock else "accelerated"
        print(
            f"Allocator resilience qualification · {config.hours:g}h {clock} · "
            f"{config.cycles} cycles at {config.interval_seconds:g}s"
        )
        print("Production controller/reconciler/persistence; modeled logical nodes and relay boundary.")
        print(f"state={root}\n")
    report = run_resilience_soak(config, root, resume=args.resume, progress=progress)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"\nResult: {'PASS' if report.passed else 'FAIL'}")
        print(
            f"  cycles={report.completed_cycles} faults={len(report.events)} "
            f"checks={sum(report.checks.values())} failures={len(report.failures)}"
        )
        print(f"  final controller term={report.final_controller_term}")
        print(f"  report={report.report_path}")
        if report.failures:
            for failure in report.failures[:10]:
                print(
                    f"  FAIL cycle {failure['cycle']} {failure['check']}: {failure['detail']}"
                )
    return 0 if report.passed else 1


def _default_root() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return paths.grid_home() / "allocator-resilience" / stamp
