#!/usr/bin/env python3
"""Build the agent-layer pattern diagrams.

Every figure in docs/agent_orchestration_patterns/images/ is generated from the
same token set as the model layer (imported, not copied), so the two catalogs
share one geometry, one palette, one type scale, and cannot drift apart. The
register is the same: green does work, purple decides, coral is the entry and
exit, solid arrows run forward, dashed arrows are the return paths that make a
pattern stateful. See docs/STYLE.md and docs/DIAGRAMS.md.

Every worker node in the agent layer is tagged with a harness x model pairing:
the harness is the node label, the model is the sub line beneath it. That is
the whole routing decision -- "which harness, with which model, doing what" --
drawn on the node instead of left to prose. The model names are an illustrative
local roster (see the README's "A word on the examples"): the router dispatches
by model name to whatever is resident on its own hardware.

Usage:  python3 docs/agent_orchestration_patterns/build_diagrams.py
"""
from __future__ import annotations
import importlib.util
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODEL = os.path.join(_HERE, "..", "model_orchestration_patterns", "build_diagrams.py")
_spec = importlib.util.spec_from_file_location("model_build", os.path.normpath(_MODEL))
_model = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_model)

Diagram = _model.Diagram
FONT = _model.FONT
node_w = _model.node_w
text_w = _model.text_w
PAD = _model.PAD
EDGE_FS = _model.EDGE_FS
H = _model.H
OUT = os.path.join(_HERE, "images")


def wfit(label, sub=None):
    """Widen a node so the harness label *and* the model subline both fit."""
    w = node_w(label)
    if sub:
        w = max(w, text_w(sub, EDGE_FS) + 2 * PAD)
    return int(round(w))


def act_gate():
    d = Diagram("The act-gate — N−1 read-only, one actor")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("r1", "Hermes ACP", "work", row=0, stage=2,
            sub="glm-4.6 · read-only", w=wfit("Hermes ACP", "glm-4.6 · read-only"),
            note="full session, gated")
    d.place("r2", "Codex", "work", row=1, stage=2,
            sub="qwen3-coder · sandbox", w=wfit("Codex", "qwen3-coder · sandbox"))
    d.place("r3", "OpenClaw", "work", row=2, stage=2,
            sub="qwen3-coder · quorum", w=wfit("OpenClaw", "qwen3-coder · quorum"))
    d.place("sel", "select", "decide", row=1, stage=3)
    d.place("actor", "Claude Code", "work", row=3, stage=3,
            sub="qwen36-35b · tools on", w=wfit("Claude Code", "qwen36-35b · tools on"),
            note="the only one that may act")
    d.place("act", "act", "terminal", row=3, stage=4,
            note="one idempotent mutation, round_id-keyed")
    d.edge("job", "dot")
    d.edge("dot", "r1", "N−1 may read", lp=(256, 74))
    d.edge("dot", "r2")
    d.edge("dot", "r3")
    d.edge("r1", "sel")
    d.edge("r2", "sel")
    d.edge("r3", "sel")
    d.edge("sel", "actor", "the one", v=True)
    d.edge("actor", "act", "the write step")
    return d


def lifecycle():
    d = Diagram("Session lifecycle — four transitions, one resident session")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("seat", "Claude Code", "work", row=1, stage=1,
            sub="qwen36-35b · resident", w=wfit("Claude Code", "qwen36-35b · resident"),
            note="resident session")
    d.place("warm", "Hermes ACP", "work", row=0, stage=2,
            sub="glm-4.6 · warm", w=wfit("Hermes ACP", "glm-4.6 · warm"),
            note="the layer's cache")
    d.place("hand", "Codex", "work", row=1, stage=2,
            sub="qwen3-coder · hand", w=wfit("Codex", "qwen3-coder · hand"),
            note="same harness only")
    d.place("kill", "OpenClaw", "work", row=2, stage=2,
            sub="qwen3-coder · kill", w=wfit("OpenClaw", "qwen3-coder · kill"),
            note="frees the seat")
    d.place("snap", "snapshot", "work", row=1, stage=3,
            note="round_id-frozen, off-box")
    d.edge("job", "seat", "spawn (cold)")
    d.edge("seat", "warm", "resume")
    d.edge("seat", "hand", "duplicate context")
    d.edge("seat", "kill", "cancel")
    d.edge("warm", "snap", "freeze")
    d.edge("hand", "snap", "checkpoint")
    return d


def lanes():
    d = Diagram("Route across harness lanes — role → lane → gate")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("pick", "pick lane", "decide", row=1, stage=1, note="residency first")
    d.place("l1", "Codex", "work", row=0, stage=2,
            sub="qwen36-27b · exec", w=wfit("Codex", "qwen36-27b · exec"),
            note="bounded coding")
    d.place("l2", "Claude Code", "work", row=1, stage=2,
            sub="qwen36-35b · stream", w=wfit("Claude Code", "qwen36-35b · stream"),
            note="open work")
    d.place("l3", "Hermes ACP", "work", row=2, stage=2,
            sub="laguna-s-2.1 · ACP", w=wfit("Hermes ACP", "laguna-s-2.1 · ACP"),
            note="verifiable")
    d.place("l4", "OpenClaw fan", "work", row=3, stage=2,
            sub="qwen3-coder · N copies", w=wfit("OpenClaw fan", "qwen3-coder · N copies"),
            note="N copies")
    d.place("ans", "answer", "terminal", row=1, stage=3)
    d.edge("job", "pick", "each request")
    d.edge("pick", "l1", "act contract binds", lp=(470, 74))
    d.edge("pick", "l2")
    d.edge("pick", "l3")
    d.edge("pick", "l4")
    d.edge("l1", "ans")
    d.edge("l2", "ans")
    d.edge("l3", "ans")
    d.edge("l4", "ans")
    return d


def seat_exec():
    d = Diagram("The seat is the executor — background only in the idle")
    d.place("job", "job", "terminal", row=0, stage=0)
    d.place("live", "Claude Code", "work", row=0, stage=1,
            sub="qwen36-35b · live", w=wfit("Claude Code", "qwen36-35b · live"),
            note="deadline-bearing")
    d.place("ans", "answer", "terminal", row=0, stage=2)
    d.place("bg", "OpenClaw fan", "work", row=2, stage=1,
            sub="qwen3-coder · pool", w=wfit("OpenClaw fan", "qwen3-coder · pool"),
            note="shadow · warm · probe")
    d.place("pre", "preempt", "decide", row=2, stage=2,
            note="evict to snapshot, fsync first")
    d.edge("job", "live")
    d.edge("live", "ans")
    d.edge("bg", "pre")
    d.edge("pre", "live", "yields to snapshot", below=40)
    return d


def admission():
    d = Diagram("Staged admission — shadow before it may act")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("inc", "Claude Code", "work", row=0, stage=1,
            sub="qwen36-35b · resident", w=wfit("Claude Code", "qwen36-35b · resident"))
    d.place("can", "Hermes ACP", "work", row=2, stage=1,
            sub="laguna-s-2.1 · shadow", w=wfit("Hermes ACP", "laguna-s-2.1 · shadow"),
            note="read-only shells only")
    d.place("cmp", "score", "decide", row=1, stage=2,
            note="vs the ground-truth authority")
    d.place("gate", "gate", "decide", row=1, stage=3, note="≥N wins at ≥X%")
    d.place("ans", "act step", "terminal", row=1, stage=4)
    d.edge("job", "inc", "live traffic")
    d.edge("job", "can", "shadow", dashed=True)
    d.edge("inc", "cmp")
    d.edge("can", "cmp")
    d.edge("cmp", "gate", "learns the bar")
    d.edge("gate", "ans", "promoted to act")
    return d


def verifier():
    d = Diagram("The verifier is ground truth, not a session")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("draft", "Hermes ACP", "work", row=1, stage=1,
            sub="qwen36-27b · draft", w=wfit("Hermes ACP", "qwen36-27b · draft"))
    d.place("check", "Codex exec", "work", row=1, stage=2,
            sub="test · schema — a fact", w=wfit("Codex exec", "test · schema — a fact"))
    d.place("cons", "Debby", "work", row=3, stage=2,
            sub="glm-4.6 · two tails", w=wfit("Debby", "glm-4.6 · two tails"),
            note="two tails agree")
    d.place("ans", "answer", "terminal", row=1, stage=3)
    d.edge("job", "draft", "one try")
    d.edge("draft", "check")
    d.edge("check", "ans", "certified")
    d.edge("draft", "cons", "no fact to offer", lp=(760, 470))
    d.edge("cons", "check", "proposed only", dashed=True, lp=(985, 385))
    return d


def ledger():
    d = Diagram("Only one ledger — the fsync'd box is the only truth")
    d.place("e1", "Claude Code", "work", row=0, stage=0,
            sub="qwen36-35b · act", w=wfit("Claude Code", "qwen36-35b · act"))
    d.place("e2", "Hermes ACP", "work", row=1, stage=0,
            sub="glm-4.6 · graduate", w=wfit("Hermes ACP", "glm-4.6 · graduate"))
    d.place("e3", "OpenClaw", "work", row=2, stage=0,
            sub="qwen3-coder · deny", w=wfit("OpenClaw", "qwen3-coder · deny"))
    d.place("app", "append", "decide", row=1, stage=1, note="round_id-stamped events")
    d.place("wal", "WAL", "deck", row=1, stage=2, sub="one box",
            note="append-only · no replication")
    d.place("exp", "export", "work", row=1, stage=3, note="different medium")
    d.edge("e1", "app")
    d.edge("e2", "app")
    d.edge("e3", "app")
    d.edge("app", "wal", "events, not state")
    d.edge("wal", "exp", "off-box, on a cadence")
    return d


def e2e():
    """A whole multi-agent system on one local box, every step named.

    The worked use case: OpenClaw fans out a defect to N agents reading the
    same repo in parallel worktrees; the act-gate lets one Claude Code seat
    write; the verifier is ground truth. No cloud, no rate limit, free tokens.
    """
    d = Diagram("One local box — a defect, N agents, one writer")
    d.place("job", "defect", "terminal", row=1, stage=0)
    d.place("fan", "OpenClaw", "work", row=1, stage=1,
            sub="qwen3-coder · fan", w=wfit("OpenClaw", "qwen3-coder · fan"),
            note="N read-only shells, own worktree")
    d.place("a1", "Codex", "work", row=0, stage=2,
            sub="qwen36-27b · repro", w=wfit("Codex", "qwen36-27b · repro"))
    d.place("a2", "Hermes ACP", "work", row=1, stage=2,
            sub="glm-4.6 · fix", w=wfit("Hermes ACP", "glm-4.6 · fix"))
    d.place("a3", "OpenCode", "work", row=2, stage=2,
            sub="qwen36-35b · fix", w=wfit("OpenCode", "qwen36-35b · fix"))
    d.place("rev", "reviewer", "decide", row=1, stage=3,
            sub="cross-vendor", w=wfit("reviewer", "cross-vendor"),
            note="a different lane than the fix, never itself")
    d.place("act", "Claude Code", "work", row=1, stage=4,
            sub="qwen36-35b · the one writer", w=wfit("Claude Code", "qwen36-35b · the one writer"),
            note="round_id-keyed, fsync first")
    d.place("ver", "Codex exec", "work", row=1, stage=5,
            sub="test · is a fact", w=wfit("Codex exec", "test · is a fact"))
    d.place("ans", "shipped fix", "terminal", row=1, stage=6,
            note="tokens free · no rate limit · on your box")
    d.edge("job", "fan")
    d.edge("fan", "a1")
    d.edge("fan", "a2")
    d.edge("fan", "a3")
    d.edge("a1", "rev")
    d.edge("a2", "rev")
    d.edge("a3", "rev")
    d.edge("rev", "act", "the one")
    d.edge("act", "ver", "the write step")
    d.edge("ver", "ans", "certified")
    return d


BUILDERS = {
    "act_gate": act_gate,
    "lifecycle": lifecycle,
    "lanes": lanes,
    "seat": seat_exec,
    "admission": admission,
    "verifier": verifier,
    "ledger": ledger,
    "e2e": e2e,
}


def build_index():
    """Compose every pattern figure onto one tall canvas, as the model layer does."""
    gap = 90
    svgs = [(name, fn().render()) for name, fn in BUILDERS.items()]
    body = []
    cursor = 40
    for _, svg in svgs:
        h = int(re.search(r'viewBox="0 0 (\d+) (\d+)"', svg).group(2))
        body.append(f'<g transform="translate(30,{cursor})">')
        body.append(_model._strip(svg))
        body.append('</g>')
        cursor = cursor + h + gap
    widths = [int(re.search(r'viewBox="0 0 (\d+) (\d+)"', s).group(1)) for _, s in svgs]
    total_w = max(widths) + 80
    total_h = cursor + 40
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w} {total_h}" '
           f'font-family="{FONT}">']
    out.append(f'<rect width="{total_w}" height="{total_h}" fill="#fff"/>')
    out.extend(body)
    out.append('</svg>')
    return "\n".join(out)


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, fn in BUILDERS.items():
        d = fn()
        probs = d.verify()
        svg = d.render()
        with open(os.path.join(OUT, name + ".svg"), "w") as f:
            f.write(svg)
        w = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        status = "ok" if not probs else "PROBLEMS: " + str(probs)
        print(f"wrote {name}.svg  [{w.group(1)}x{w.group(2)}]  verify {status}")
    with open(os.path.join(OUT, "index.svg"), "w") as f:
        f.write(build_index())
    print("wrote index.svg")


if __name__ == "__main__":
    main()
