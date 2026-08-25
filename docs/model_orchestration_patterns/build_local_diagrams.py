#!/usr/bin/env python3
"""Build three local-native patterns, two foundation figures, and an overview.

The drawing primitives and palette come from build_diagrams.py so the concise
catalog and the portable research archive remain one visual family. Individual
figures contain no visible title; the README heading is their caption.
"""
from __future__ import annotations

import os
from build_diagrams import Diagram


OUT = os.path.join(os.path.dirname(__file__), "images")


def artifact_contract():
    d = Diagram("Model Artifact Contract — route to a build, not a name")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("resolve", "resolve contract", "decide", row=1, stage=1)
    d.place("gate", "provenance + eval", "decide", row=1, stage=2)
    d.place("exact", "exact build", "work", row=0, stage=3,
            sub="weights · quant · runtime", w=380)
    d.place("trusted", "re-admit fallback", "work", row=2, stage=3,
            sub="otherwise refuse", w=300)
    d.place("out", "answer / refuse", "terminal", row=1, stage=4, w=280)
    d.edge("job", "resolve", "role")
    d.edge("resolve", "gate", "contract id")
    d.edge("gate", "exact", "admit")
    d.edge("gate", "trusted", "candidate fails")
    d.edge("exact", "out")
    d.edge("trusted", "out")
    return d


def resident_set():
    d = Diagram("Resident-Set Planner — plan from what actually fits in memory", m_l=160)
    d.place("inventory", "live inventory", "work", row=0, stage=0,
            sub="weights · KV · seats", w=300)
    d.place("graph", "logical graph", "terminal", row=2, stage=0)
    d.place("plan", "plan resident set", "decide", row=1, stage=1)
    d.place("warm", "reuse warm", "work", row=0, stage=2)
    d.place("cold", "budgeted load", "work", row=1, stage=2)
    d.place("degrade", "admitted degrade", "work", row=2, stage=2, w=290)
    d.place("stop", "queue / refuse", "terminal", row=3, stage=2, w=260)
    d.place("run", "physical coordinator", "decide", row=1, stage=3,
            sub="reserve once · dispatch", w=360)
    d.place("out", "answer / refuse", "terminal", row=1, stage=4, w=280)
    d.edge("inventory", "plan", "snapshot")
    d.edge("graph", "plan", "requirements")
    d.edge("plan", "warm")
    d.edge("plan", "cold")
    d.edge("plan", "degrade")
    d.edge("plan", "stop")
    d.edge("warm", "run")
    d.edge("cold", "run")
    d.edge("degrade", "run")
    d.edge("run", "out")
    return d


def boundary_compiled_graph():
    d = Diagram("Boundary-Compiled Graph — compile egress before compute", m_l=150)
    d.place("job", "labeled job", "terminal", row=1, stage=0)
    d.place("policy", "boundary policy", "work", row=2, stage=0, w=280)
    d.place("constrain", "constrain graph", "decide", row=1, stage=1)
    d.place("resolved", "resolved components", "work", row=1, stage=2,
            sub="F1 models · tools · sinks", w=380)
    d.place("compile", "bind manifest", "decide", row=1, stage=3)
    d.place("local", "approved local graph", "work", row=0, stage=4, w=360)
    d.place("gate", "redact + authorize", "decide", row=2, stage=4, w=320)
    d.place("external", "visible external edge", "work", row=2, stage=5, w=380)
    d.place("out", "answer", "terminal", row=0, stage=6)
    d.place("stop", "defer / refuse", "terminal", row=3, stage=5, w=260)
    d.edge("job", "constrain", "data class")
    d.edge("policy", "constrain", "allowed sinks")
    d.edge("constrain", "resolved", "eligible")
    d.edge("resolved", "compile", "exact graph")
    d.edge("compile", "local", "inside policy")
    d.edge("compile", "gate", "proposed egress")
    d.edge("local", "out")
    d.edge("gate", "external", "allowed")
    d.edge("external", "out", "manifested")
    d.edge("gate", "stop")
    return d


def night_shift():
    d = Diagram("Verified Night Shift — improve the box while it sleeps", m_l=180)
    d.place("live", "live arrival", "terminal", row=0, stage=0)
    d.place("foreground", "foreground seat", "work", row=0, stage=2)
    d.place("answer", "answer", "terminal", row=0, stage=3)
    d.place("backlog", "improvement backlog", "terminal", row=2, stage=0,
            w=350)
    d.place("idle", "idle eligible?", "decide", row=2, stage=1)
    d.place("quantum", "bounded quantum", "work", row=2, stage=2)
    d.place("policy", "trusted type policy", "work", row=1, stage=4, w=370)
    d.place("stage", "immutable staged result", "work", row=2, stage=3, w=450)
    d.place("stop", "checkpoint / abort", "terminal", row=3, stage=3, w=320)
    d.place("eval", "typed validator", "decide", row=2, stage=4)
    d.place("retain", "retain live", "terminal", row=3, stage=5, w=220)
    d.place("promote", "atomic promotion", "work", row=2, stage=5, w=300)
    d.edge("live", "foreground")
    d.edge("foreground", "answer")
    d.edge("backlog", "idle")
    d.edge("idle", "quantum", "headroom")
    d.edge("quantum", "foreground", "yield within bound", thick=True)
    d.edge("quantum", "stop", "preempt")
    d.edge("quantum", "stage", "complete")
    d.edge("stage", "eval", "exact digest")
    d.edge("policy", "eval", v=True)
    d.edge("policy", "promote")
    d.edge("eval", "promote", "pass")
    d.edge("eval", "retain", "fail")
    return d


def energy_envelope():
    d = Diagram("Energy Envelope — spend joules, not tokens")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("signals", "host signals", "work", row=0, stage=1,
            sub="power · temp · battery", w=320)
    d.place("risk", "consequence", "decide", row=2, stage=1)
    d.place("govern", "admit envelope", "decide", row=1, stage=2)
    d.place("full", "full plan", "work", row=0, stage=3)
    d.place("lean", "lean plan", "work", row=1, stage=3)
    d.place("cool", "defer / refuse", "terminal", row=2, stage=3, w=260)
    d.place("coordinate", "physical coordinator", "decide", row=1, stage=4,
            sub="reserve once · dispatch", w=360)
    d.place("run", "monitored execute", "work", row=1, stage=5, w=300)
    d.place("feedback", "measured feedback", "work", row=0, stage=6,
            sub="next calibration", w=330)
    d.place("out", "answer", "terminal", row=1, stage=6)
    d.place("stop", "defer / refuse", "terminal", row=2, stage=6, w=260)
    d.edge("job", "signals")
    d.edge("job", "risk")
    d.edge("signals", "govern")
    d.edge("risk", "govern")
    d.edge("govern", "full")
    d.edge("govern", "lean")
    d.edge("govern", "cool")
    d.edge("full", "coordinate")
    d.edge("lean", "coordinate")
    d.edge("coordinate", "run")
    d.edge("run", "feedback", "measure")
    d.edge("run", "out")
    d.edge("run", "stop", "hard limit")
    return d


def local_overview():
    d = Diagram("Three local-native patterns with two operator-control foundations",
                m_l=190)
    d.place("policy", "boundary policy", "work", row=0, stage=0, w=280)
    d.place("request", "portable graph", "terminal", row=1, stage=0, w=280)
    d.place("f2a", "F2 · constrain", "decide", row=1, stage=1, w=280)
    d.place("f1", "F1 · exact artifact", "decide", row=1, stage=2, w=380)
    d.place("components", "component registry", "work", row=0, stage=2,
            sub="tools · telemetry · storage", w=400)
    d.place("inventory", "resident inventory", "work", row=0, stage=3,
            sub="weights · KV · leases", w=360)
    d.place("f2b", "F2 · bind manifest", "decide", row=1, stage=3, w=340)
    d.place("host", "host envelope", "work", row=2, stage=3,
            sub="joules · heat · owner", w=310)
    d.place("l1", "L1 · resident set", "decide", row=0, stage=4, w=310)
    d.place("l3", "L3 · energy", "decide", row=2, stage=4, w=250)
    d.place("execute", "physical coordinator", "decide", row=1, stage=5,
            sub="reserve once · dispatch", w=380)
    d.place("out", "answer / defer / refuse", "terminal", row=1, stage=6, w=450)

    d.place("backlog", "typed improvement", "terminal", row=3, stage=0,
            w=320)
    d.place("l2", "L2 · verified night shift", "decide", row=3, stage=1,
            sub="borrowed L1 + L3 lease", w=480)
    d.place("candidate", "validated F1 candidate", "work", row=3, stage=2,
            w=390)

    d.edge("policy", "f2a", "enforce")
    d.edge("request", "f2a")
    d.edge("f2a", "f1", "allowed roles")
    d.edge("f1", "f2b", "exact graph")
    d.edge("components", "f2b")
    d.edge("inventory", "l1")
    d.edge("host", "l3")
    d.edge("f2b", "l1")
    d.edge("f2b", "l3")
    d.edge("l1", "execute")
    d.edge("l3", "execute")
    d.edge("execute", "out")
    d.edge("backlog", "l2")
    d.edge("l2", "candidate")
    return d


BUILDERS = {
    "local_artifact": artifact_contract,
    "local_resident": resident_set,
    "local_boundary": boundary_compiled_graph,
    "local_night": night_shift,
    "local_energy": energy_envelope,
}


def main():
    os.makedirs(OUT, exist_ok=True)
    failures = {}
    for name, builder in BUILDERS.items():
        diagram = builder()
        problems = diagram.verify()
        svg = diagram.render()
        with open(os.path.join(OUT, name + ".svg"), "w") as handle:
            handle.write(svg)
        if problems:
            failures[name] = problems
        state = "ok" if not problems else f"PROBLEMS: {problems}"
        print(f"wrote {name}.svg {state}")

    overview = local_overview()
    problems = overview.verify()
    with open(os.path.join(OUT, "local_index.svg"), "w") as handle:
        handle.write(overview.render())
    if problems:
        failures["local_index"] = problems
    print(f"wrote local_index.svg {'ok' if not problems else problems}")
    if failures:
        raise SystemExit(f"diagram verification failed: {failures}")


if __name__ == "__main__":
    main()
