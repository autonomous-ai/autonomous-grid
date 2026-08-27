#!/usr/bin/env python3
"""Catalog diagrams for choosing, comparing, dividing, and reusing work.

Each figure uses the shared model-orchestration diagram grammar.  The title is
kept in the SVG for accessibility; the surrounding Markdown supplies the
visible pattern heading.
"""
from __future__ import annotations

from catalog_diagram import Diagram


def best_fit():
    d = Diagram("Best Fit — use the smallest adequate model", m_l=180)
    d.place("job", "request", "terminal", row=1, stage=0)
    d.place("choose", "fit to roster", "decide", row=1, stage=1)
    d.place("small", "small model", "work", row=0, stage=2)
    d.place("large", "larger model", "work", row=2, stage=2)
    d.place("out", "answer", "terminal", row=1, stage=3)
    d.edge("job", "choose", "classify")
    d.edge("choose", "small", "adequate")
    d.edge("choose", "large", "needs more")
    d.edge("small", "out")
    d.edge("large", "out")
    return d


def recipe_router():
    d = Diagram("Recipe Router — choose the workflow before work begins", m_l=180)
    d.place("job", "request", "terminal", row=1, stage=0)
    d.place("choose", "choose recipe", "decide", row=1, stage=1)
    d.place("quick", "quick answer", "work", row=0, stage=2)
    d.place("checked", "checked answer", "work", row=1, stage=2)
    d.place("split", "split job", "work", row=2, stage=2)
    d.place("out", "result", "terminal", row=1, stage=3)
    d.edge("job", "choose", "classify")
    d.edge("choose", "quick", "simple")
    d.edge("choose", "checked", "risky")
    d.edge("choose", "split", "large")
    d.edge("quick", "out")
    d.edge("checked", "out")
    d.edge("split", "out")
    return d


def adaptive_effort():
    d = Diagram("Adaptive Effort — spend more only while uncertainty remains", m_l=180)
    d.place("job", "request", "terminal", row=1, stage=0)
    d.place("attempt", "small attempt", "work", row=1, stage=1)
    d.place("gap", "still uncertain?", "decide", row=1, stage=2)
    d.place("out", "answer", "terminal", row=0, stage=3)
    d.place("more", "add effort", "work", row=2, stage=3)
    d.edge("job", "attempt")
    d.edge("attempt", "gap", "check")
    d.edge("gap", "out", "enough")
    d.edge("gap", "more", "uncertain")
    d.edge("more", "gap", dashed=True, below=54)
    return d


def risk_ladder():
    d = Diagram("Risk Ladder — raise the proof bar with consequence", m_l=180)
    d.place("job", "request", "terminal", row=1, stage=0)
    d.place("risk", "classify risk", "decide", row=1, stage=1)
    d.place("light", "one pass", "work", row=0, stage=2)
    d.place("checked", "attempts + checks", "work", row=1, stage=2)
    d.place("rigorous", "checks + evidence", "work", row=2, stage=2)
    d.place("out", "answer", "terminal", row=1, stage=3)
    d.edge("job", "risk", "consequence")
    d.edge("risk", "light", "low")
    d.edge("risk", "checked", "medium")
    d.edge("risk", "rigorous", "high")
    d.edge("light", "out")
    d.edge("checked", "out")
    d.edge("rigorous", "out")
    return d


def routing_memory():
    d = Diagram("Routing Memory — remember which route works", m_l=180)
    d.place("outcome", "verified outcome", "terminal", row=2, stage=0)
    d.place("job", "next request", "terminal", row=0, stage=1)
    d.place("history", "route history", "work", row=2, stage=1)
    d.place("choose", "choose route", "decide", row=1, stage=2)
    d.place("run", "run route", "work", row=1, stage=3)
    d.place("out", "answer", "terminal", row=1, stage=4)
    d.edge("outcome", "history")
    d.edge("job", "choose")
    d.edge("history", "choose")
    d.edge("choose", "run")
    d.edge("run", "out")
    return d


def brute_force():
    d = Diagram("Brute Force — try many ways and keep the proven winner", m_l=180)
    d.place("goal", "goal", "terminal", row=1, stage=0)
    d.place("fan", "", "dot", row=1, stage=1)
    d.place("a", "different path A", "work", row=0, stage=2)
    d.place("b", "different path B", "work", row=1, stage=2)
    d.place("c", "different path C", "work", row=2, stage=2)
    d.place("test", "test + select", "decide", row=1, stage=3)
    d.place("winner", "best passing", "terminal", row=1, stage=4)
    d.edge("goal", "fan", "same goal")
    d.edge("fan", "a")
    d.edge("fan", "b")
    d.edge("fan", "c")
    d.edge("a", "test")
    d.edge("b", "test")
    d.edge("c", "test")
    d.edge("test", "winner")
    return d


def check_and_retry():
    d = Diagram("Check and Retry — make failed checks useful", m_l=180)
    d.place("job", "request", "terminal", row=1, stage=0)
    d.place("draft", "draft / repair", "work", row=1, stage=1)
    d.place("check", "check", "decide", row=1, stage=2)
    d.place("out", "verified answer", "terminal", row=0, stage=3)
    d.place("stop", "defer / refuse", "terminal", row=2, stage=3)
    d.edge("job", "draft", "attempt")
    d.edge("draft", "check")
    d.edge("check", "out", "pass")
    d.edge("check", "draft", "failure evidence", dashed=True, below=54)
    d.edge("check", "stop", "limit / unsafe")
    return d


def vote():
    d = Diagram("Vote — use a majority or abstain", m_l=180)
    d.place("question", "discrete question", "terminal", row=1, stage=0)
    d.place("fan", "", "dot", row=1, stage=1)
    d.place("a", "independent answer", "work", row=0, stage=2)
    d.place("b", "independent answer", "work", row=1, stage=2)
    d.place("c", "independent answer", "work", row=2, stage=2)
    d.place("count", "majority?", "decide", row=1, stage=3)
    d.place("out", "decide / abstain", "terminal", row=1, stage=4)
    d.edge("question", "fan")
    d.edge("fan", "a")
    d.edge("fan", "b")
    d.edge("fan", "c")
    d.edge("a", "count")
    d.edge("b", "count")
    d.edge("c", "count")
    d.edge("count", "out")
    return d


def challenge():
    d = Diagram("Challenge — give every important answer a skeptic", m_l=180)
    d.place("draft", "proposed answer", "terminal", row=1, stage=0)
    d.place("skeptic", "independent challenge", "work", row=1, stage=1)
    d.place("resolve", "resolve objections", "decide", row=1, stage=2)
    d.place("answer", "qualified answer", "terminal", row=0, stage=3)
    d.place("abstain", "abstain", "terminal", row=2, stage=3)
    d.edge("draft", "skeptic")
    d.edge("skeptic", "resolve")
    d.edge("resolve", "answer")
    d.edge("resolve", "abstain")
    return d


def diversity_gate():
    d = Diagram("Diversity Gate — keep only genuinely different paths", m_l=180)
    d.place("source", "candidate stream", "terminal", row=1, stage=0)
    d.place("a", "model-family path", "work", row=0, stage=1)
    d.place("b", "evidence path", "work", row=1, stage=1)
    d.place("c", "alternate method", "work", row=2, stage=1)
    d.place("gate", "distinct path?", "decide", row=1, stage=2)
    d.place("keep", "diverse set", "terminal", row=0, stage=3)
    d.place("reject", "reject clone", "terminal", row=2, stage=3)
    d.edge("source", "a")
    d.edge("source", "b")
    d.edge("source", "c")
    d.edge("a", "gate")
    d.edge("b", "gate")
    d.edge("c", "gate")
    d.edge("gate", "keep", "distinct")
    d.edge("gate", "reject", "same path", dashed=True)
    return d


def tiebreaker():
    d = Diagram("Tiebreaker — break a split with new evidence", m_l=180)
    d.place("split", "split vote", "terminal", row=1, stage=0)
    d.place("tool", "objective tool", "work", row=0, stage=1)
    d.place("judge", "different judge", "work", row=2, stage=1)
    d.place("resolve", "compare finalists", "decide", row=1, stage=2)
    d.place("out", "decide / abstain", "terminal", row=1, stage=3)
    d.edge("split", "tool", "tool available")
    d.edge("split", "judge", "otherwise")
    d.edge("tool", "resolve", "evidence")
    d.edge("judge", "resolve", "independent read")
    d.edge("resolve", "out", "clear / tied")
    return d


def ensemble():
    d = Diagram("Ensemble — combine numeric estimates robustly", m_l=180)
    d.place("question", "numeric question", "terminal", row=1, stage=0)
    d.place("fan", "", "dot", row=1, stage=1)
    d.place("a", "estimate", "work", row=0, stage=2)
    d.place("b", "estimate", "work", row=1, stage=2)
    d.place("c", "estimate", "work", row=2, stage=2)
    d.place("median", "median", "decide", row=1, stage=3)
    d.place("out", "numeric result", "terminal", row=1, stage=4)
    d.edge("question", "fan", "estimate independently")
    d.edge("fan", "a")
    d.edge("fan", "b")
    d.edge("fan", "c")
    d.edge("a", "median")
    d.edge("b", "median")
    d.edge("c", "median")
    d.edge("median", "out")
    return d


def blind_estimate():
    d = Diagram("Blind Estimate — estimate alone before seeing the group", m_l=180)
    d.place("question", "question", "terminal", row=1, stage=0)
    d.place("a", "private estimate A", "work", row=0, stage=1)
    d.place("b", "private estimate B", "work", row=2, stage=1)
    d.place("summary", "group summary", "decide", row=1, stage=2)
    d.place("revise", "one revision", "work", row=1, stage=3)
    d.place("out", "final estimates", "terminal", row=1, stage=4)
    d.edge("question", "a")
    d.edge("question", "b")
    d.edge("a", "summary")
    d.edge("b", "summary")
    d.edge("summary", "revise")
    d.edge("revise", "out")
    return d


def split_work():
    d = Diagram("Split Work — divide by responsibility and merge", m_l=180)
    d.place("job", "large job", "terminal", row=1, stage=0)
    d.place("split", "split responsibilities", "decide", row=1, stage=1)
    d.place("a", "part A specialist", "work", row=0, stage=2)
    d.place("b", "part B specialist", "work", row=1, stage=2)
    d.place("c", "part C specialist", "work", row=2, stage=2)
    d.place("merge", "merge parts", "decide", row=1, stage=3)
    d.place("out", "result", "terminal", row=1, stage=4)
    d.edge("job", "split")
    d.edge("split", "a")
    d.edge("split", "b")
    d.edge("split", "c")
    d.edge("a", "merge")
    d.edge("b", "merge")
    d.edge("c", "merge")
    d.edge("merge", "out")
    return d


def pipeline():
    d = Diagram("Pipeline — pass work through a fixed sequence", m_l=180)
    d.place("input", "input", "terminal", row=1, stage=0)
    d.place("a", "stage A", "work", row=1, stage=1)
    d.place("b", "stage B", "work", row=1, stage=2)
    d.place("c", "stage C", "work", row=1, stage=3)
    d.place("out", "output", "terminal", row=1, stage=4)
    d.edge("input", "a", "handoff")
    d.edge("a", "b")
    d.edge("b", "c")
    d.edge("c", "out", "result")
    return d


def answer_cache():
    d = Diagram("Answer Cache — reuse a verified answer until sources change", m_l=180)
    d.place("job", "request", "terminal", row=1, stage=0)
    d.place("cache", "fingerprint + lookup", "decide", row=1, stage=1)
    d.place("hit", "cached answer", "terminal", row=0, stage=2)
    d.place("compute", "compute answer", "work", row=2, stage=2)
    d.place("verify", "verify + store", "decide", row=2, stage=3)
    d.place("fresh", "verified answer", "terminal", row=1, stage=4)
    d.edge("job", "cache")
    d.edge("cache", "hit")
    d.edge("cache", "compute")
    d.edge("compute", "verify")
    d.edge("verify", "fresh")
    d.edge("verify", "cache", dashed=True, below=56)
    return d


REASONING_BUILDERS = {
    "best_fit": best_fit,
    "recipe_router": recipe_router,
    "adaptive_effort": adaptive_effort,
    "risk_ladder": risk_ladder,
    "routing_memory": routing_memory,
    "brute_force": brute_force,
    "check_and_retry": check_and_retry,
    "vote": vote,
    "challenge": challenge,
    "diversity_gate": diversity_gate,
    "tiebreaker": tiebreaker,
    "ensemble": ensemble,
    "blind_estimate": blind_estimate,
    "split_work": split_work,
    "pipeline": pipeline,
    "answer_cache": answer_cache,
}
