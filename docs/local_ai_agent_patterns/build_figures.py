#!/usr/bin/env python3
"""Build figures for the unified local-first agent pattern language."""
from __future__ import annotations

import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent
MODEL_PATTERNS = HERE.parent / "model_orchestration_patterns"
sys.path.insert(0, str(MODEL_PATTERNS))

from catalog_diagram import Diagram  # noqa: E402


OUT = HERE / "images"


def whole_system() -> Diagram:
    """One request crossing all four planes of a local-first agent."""
    d = Diagram(
        "A local-first agent: owned context enters a bounded workflow, "
        "evidence is verified, one action is gated, and outcomes return "
        "through a durable ledger",
        m_l=150,
        stage_gap=55,
    )
    d.place("request", "request", "terminal", row=0, stage=0)
    d.place("context", "private context", "work", row=2, stage=0)
    d.place("boundary", "boundary + risk", "decide", row=1, stage=1)
    d.place("route", "route + budget", "decide", row=1, stage=2)
    d.place("box", "live box", "work", row=3, stage=2)
    d.place("work", "workers", "deck", row=1, stage=3)
    d.place("verify", "verifier", "decide", row=1, stage=4)
    d.place("gate", "act gate", "decide", row=1, stage=5)
    d.place("outcome", "outcome", "terminal", row=0, stage=6)
    d.place("ledger", "ledger", "work", row=2, stage=6)
    d.edge("request", "boundary")
    d.edge("context", "boundary")
    d.edge("boundary", "route")
    d.edge("route", "work")
    d.edge("box", "work")
    d.edge("work", "verify")
    d.edge("verify", "gate")
    d.edge("gate", "outcome")
    d.edge("outcome", "ledger", v=True)
    d.edge("ledger", "route", dashed=True, below=58)
    return d


def tool_boundary() -> Diagram:
    """A narrow typed capability surface between reasoning and execution."""
    d = Diagram(
        "Typed Tool Boundary — discover one narrow capability, gate mutation, "
        "execute outside the reasoning loop, and return a typed result",
        m_l=150,
        stage_gap=110,
    )
    d.place("agent", "agent", "terminal", row=1, stage=0)
    d.place("choose", "choose capability", "decide", row=1, stage=1)
    d.place("read", "read tool", "work", row=0, stage=2)
    d.place("gate", "act gate", "decide", row=2, stage=2)
    d.place("execute", "execute", "work", row=1, stage=3)
    d.place("result", "typed result", "terminal", row=1, stage=4)
    d.edge("agent", "choose")
    d.edge("choose", "read", "read")
    d.edge("choose", "gate", "mutate")
    d.edge("read", "execute")
    d.edge("gate", "execute")
    d.edge("execute", "result")
    return d


BUILDERS = {
    "whole_system": whole_system,
    "tool_boundary": tool_boundary,
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    failures = {}
    for name, builder in BUILDERS.items():
        filename = f"{name}.svg"
        try:
            diagram = builder()
            problems = diagram.verify()
            (OUT / filename).write_text(diagram.render(), encoding="utf-8")
            if problems:
                failures[name] = problems
                print(f"wrote {filename} PROBLEMS: {problems}")
            else:
                print(f"wrote {filename} ok")
        except Exception as exc:  # Report every figure in one run.
            failures[name] = [f"{type(exc).__name__}: {exc}"]
            print(f"failed {filename}: {type(exc).__name__}: {exc}")

    if failures:
        print(f"unified figure verification failed: {failures}")
        return 1
    print(f"wrote {len(BUILDERS)} unified figures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
