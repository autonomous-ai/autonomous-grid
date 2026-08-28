#!/usr/bin/env python3
"""Build the flagship local-AI composition diagrams.

These figures use the same palette, geometry, caption fields, and verification
rules as the individual pattern catalog.  A composition is intentionally drawn
as one capability-level flow; the surrounding document names the patterns that
implement each stage.
"""
from __future__ import annotations

from pathlib import Path

from catalog_diagram import Diagram


OUT = Path(__file__).with_name("images")


def verified_search() -> Diagram:
    d = Diagram(
        "Verified Search Engine — many local attempts, one proven result",
        m_l=180,
        stage_gap=140,
    )
    d.place("goal", "difficult goal", "terminal", row=1, stage=0)
    d.place("attempts", "distinct attempts", "deck", row=1, stage=1)
    d.place("oracle", "objective oracle", "decide", row=1, stage=2)
    d.place("winner", "proven winner", "terminal", row=0, stage=3)
    d.place("repair", "repair near-pass", "work", row=2, stage=3)
    d.place("stop", "evidence + defer", "terminal", row=3, stage=3)
    d.edge("goal", "attempts", "fan out")
    d.edge("attempts", "oracle", "same test")
    d.edge("oracle", "winner", "pass")
    d.edge("oracle", "repair", "use failure evidence")
    d.edge("oracle", "stop", "budget ends")
    d.edge("repair", "oracle", dashed=True, below=58)
    return d


def live_decision_loop() -> Diagram:
    d = Diagram(
        "Live Decision Loop — fresh local context becomes one bounded action",
        m_l=180,
        stage_gap=110,
    )
    d.place("events", "live events", "terminal", row=0, stage=0)
    d.place("history", "owned history", "work", row=2, stage=0)
    d.place("context", "context window", "work", row=1, stage=1)
    d.place("trigger", "decision point?", "decide", row=1, stage=2)
    d.place("reason", "local reasoner", "work", row=0, stage=3)
    d.place("skip", "rules / no action", "terminal", row=2, stage=3)
    d.place("gate", "policy gate", "decide", row=0, stage=4)
    d.place("action", "one action", "terminal", row=1, stage=4)
    d.place("outcome", "outcome", "work", row=2, stage=4)
    d.edge("events", "context")
    d.edge("history", "context", "scoped")
    d.edge("context", "trigger")
    d.edge("trigger", "reason", "admit")
    d.edge("trigger", "skip", "skip")
    d.edge("reason", "gate", "proposal")
    d.edge("gate", "action", "allow", v=True)
    d.edge("action", "outcome", v=True)
    d.edge("outcome", "context", "learn", dashed=True, below=58)
    return d


def measured_optimization_loop() -> Diagram:
    d = Diagram(
        "Measured Optimization Loop — propose broadly, promote by outcomes",
        m_l=180,
        stage_gap=110,
    )
    d.place("goal", "objective", "terminal", row=1, stage=0)
    d.place("variants", "local variants", "deck", row=1, stage=1)
    d.place("checks", "preflight", "decide", row=0, stage=2)
    d.place("traffic", "controlled traffic", "work", row=1, stage=2)
    d.place("evidence", "enough evidence?", "decide", row=1, stage=3)
    d.place("promote", "promote", "terminal", row=0, stage=4)
    d.place("retain", "keep control", "terminal", row=2, stage=4)
    d.place("rollback", "rollback", "terminal", row=3, stage=4)
    d.edge("goal", "variants")
    d.edge("variants", "checks")
    d.edge("checks", "traffic", "valid", v=True)
    d.edge("traffic", "evidence", "outcomes")
    d.edge("evidence", "promote", "wins")
    d.edge("evidence", "retain", "uncertain")
    d.edge("evidence", "rollback", "harm")
    d.edge("evidence", "variants", "next round", dashed=True, below=58)
    return d


def private_offline_copilot() -> Diagram:
    d = Diagram(
        "Private Offline Copilot — an owned dependency path remains useful",
        m_l=190,
        stage_gap=70,
    )
    d.place("request", "request", "terminal", row=0, stage=0)
    d.place("private", "private context", "work", row=2, stage=0)
    d.place("scope", "scope + purpose", "decide", row=1, stage=1)
    d.place("stack", "owned local stack", "work", row=1, stage=2)
    d.place("enough", "complete enough?", "decide", row=1, stage=3)
    d.place("answer", "useful answer", "terminal", row=0, stage=4)
    d.place("gate", "remote policy", "decide", row=2, stage=3)
    d.place("defer", "defer", "terminal", row=3, stage=4)
    d.place("remote", "explicit fallback", "terminal", row=2, stage=4)
    d.edge("request", "scope")
    d.edge("private", "scope")
    d.edge("scope", "stack")
    d.edge("stack", "enough")
    d.edge("enough", "answer", "yes")
    d.edge("enough", "gate", "no", v=True)
    d.edge("gate", "remote", "allow")
    d.edge("gate", "defer", "stop")
    return d


BUILDERS = {
    "verified_search": verified_search,
    "live_decision_loop": live_decision_loop,
    "measured_optimization_loop": measured_optimization_loop,
    "private_offline_copilot": private_offline_copilot,
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    failures = {}
    for name, builder in BUILDERS.items():
        filename = f"composition_{name}.svg"
        try:
            diagram = builder()
            problems = diagram.verify()
            (OUT / filename).write_text(diagram.render(), encoding="utf-8")
            if problems:
                failures[name] = problems
                print(f"wrote {filename} PROBLEMS: {problems}")
            else:
                print(f"wrote {filename} ok")
        except Exception as exc:  # Report every composition in one run.
            failures[name] = [f"{type(exc).__name__}: {exc}"]
            print(f"failed {filename}: {type(exc).__name__}: {exc}")

    if failures:
        print(f"composition diagram verification failed: {failures}")
        return 1
    print(f"wrote {len(BUILDERS)} composition diagrams")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
