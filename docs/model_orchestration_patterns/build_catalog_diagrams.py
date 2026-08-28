#!/usr/bin/env python3
"""Build the 31 concise local-first catalog diagrams.

The individual builders are grouped by reasoning and runtime concerns. This
file owns their shared contract and rendering lifecycle.
"""
from __future__ import annotations

from pathlib import Path

from build_diagrams import Diagram
from catalog_diagrams_reasoning import REASONING_BUILDERS
from catalog_diagrams_runtime import RUNTIME_BUILDERS


OUT = Path(__file__).with_name("images")

EXPECTED_NAMES = (
    "best_fit",
    "recipe_router",
    "adaptive_effort",
    "risk_ladder",
    "routing_memory",
    "brute_force",
    "check_and_retry",
    "vote",
    "challenge",
    "diversity_gate",
    "tiebreaker",
    "ensemble",
    "blind_estimate",
    "split_work",
    "pipeline",
    "answer_cache",
    "shadow_model",
    "model_audition",
    "night_shift",
    "pinned_model",
    "fit_the_box",
    "keep_it_warm",
    "idle_worker",
    "power_budget",
    "straggler_backup",
    "circuit_breaker",
    "local_cascade",
    "data_stays_put",
    "privacy_boundary",
    "offline_island",
    "private_memory",
)


def catalog_builders():
    """Return the complete builder map after enforcing its exact key set."""
    duplicate = set(REASONING_BUILDERS) & set(RUNTIME_BUILDERS)
    if duplicate:
        names = ", ".join(sorted(duplicate))
        raise ValueError(f"duplicate catalog builder keys: {names}")

    builders = {**REASONING_BUILDERS, **RUNTIME_BUILDERS}
    expected = set(EXPECTED_NAMES)
    actual = set(builders)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        details = []
        if missing:
            details.append("missing: " + ", ".join(sorted(missing)))
        if extra:
            details.append("extra: " + ", ".join(sorted(extra)))
        raise ValueError("catalog builder set mismatch (" + "; ".join(details) + ")")

    return builders


def main() -> int:
    try:
        builders = catalog_builders()
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    failures = {}

    for name in EXPECTED_NAMES:
        filename = f"catalog_{name}.svg"
        try:
            diagram = builders[name]()
            if not isinstance(diagram, Diagram):
                raise TypeError(
                    f"builder returned {type(diagram).__name__}, expected Diagram"
                )

            problems = diagram.verify()
            svg = diagram.render()
            (OUT / filename).write_text(svg, encoding="utf-8")
            if problems:
                failures[name] = problems
                print(f"wrote {filename} PROBLEMS: {problems}")
            else:
                print(f"wrote {filename} ok")
        except Exception as exc:  # Report all builders instead of stopping at one.
            failures[name] = [f"{type(exc).__name__}: {exc}"]
            print(f"failed {filename}: {type(exc).__name__}: {exc}")

    if failures:
        print(f"catalog diagram verification failed: {failures}")
        return 1

    print(f"wrote {len(EXPECTED_NAMES)} catalog diagrams")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
