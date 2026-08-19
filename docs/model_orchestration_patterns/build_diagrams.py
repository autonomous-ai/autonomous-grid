#!/usr/bin/env python3
"""Build the orchestration-pattern diagrams.

Every figure in docs/model_orchestration_patterns/images/ is generated from the token
set below, so the whole family shares one geometry, one palette, one type scale,
and cannot drift apart. A diagram is data: stages (columns) of nodes, edges
between them, and the canvas is computed from what the drawing needs.

The palette is the repo-wide figure style (docs/STYLE.md): green does work,
purple decides, coral is a terminal, arrows are warm grey, and there is no
fourth hue and no decoration. The index is not a fourth drawing style -- it is
the same figures composed onto one tall page, so it cannot disagree with them.

Usage:  python3 docs/model_orchestration_patterns/build_diagrams.py
"""
from __future__ import annotations
import os

OUT = os.path.join(os.path.dirname(__file__), "images")

# --- palette (docs/STYLE.md) -------------------------------------------------
G  = dict(fill="#eff5ea", line="#b8d7a6", txt="#5a912f")   # green  - work
P  = dict(fill="#f3f2f9", line="#a9a3c9", txt="#5d579d")   # purple - decide
C  = dict(fill="#fbf0ed", line="#f0c5b6", txt="#ad5130")   # coral  - terminal
ARROW = "#a1a099"
INK   = "#2b2b29"
FONT  = "Avenir Next, Nunito, Segoe UI, Helvetica, Arial, sans-serif"

# --- geometry ----------------------------------------------------------------
H      = 66          # every node is this tall (docs/STYLE.md)
PAD    = 22          # horizontal padding inside a node (per side)
ROW    = 118         # vertical pitch between stacked rows
STAGE  = 198         # horizontal pitch between stage columns
M_L, M_R, M_T, M_B = 70, 96, 92, 88   # margins
NODE_FS = 28         # label inside a node
EDGE_FS = 23         # label on an edge

def text_w(s, fs):
    return max(1, len(s)) * fs * 0.60

def node_w(label, fs=NODE_FS):
    return max(100, text_w(label, fs) + 2 * PAD)

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# --- the layout --------------------------------------------------------------
class Diagram:
    def __init__(self, title=None, m_l=None):
        self.nodes = {}
        self.edges = []
        self._stage = 0
        self._row = 0
        self.title = title
        self.m_l = M_L if m_l is None else m_l   # left margin (widen if a stage-0 node is wide)

    def next(self, stage=None, row=None):
        """Advance cursor; returns (stage,row)."""
        if stage is not None:
            self._stage = stage
        if row is not None:
            self._row = row
        return self._stage, self._row

    def place(self, nid, label, kind="work", row=None, stage=None, w=None,
              sub=None, note=None):
        row = self._row if row is None else row
        stage = self._stage if stage is None else stage
        w = w or node_w(label)
        self.nodes[nid] = dict(label=label, kind=kind, row=row, stage=stage,
                               w=w, sub=sub, note=note)
        return nid

    def edge(self, a, b, label=None, dashed=False, color=ARROW, below=0,
             lp=None, anchor=None, thick=False, v=False):
        self.edges.append(dict(a=a, b=b, label=label, dashed=dashed,
                               color=color, below=below, lp=lp, anchor=anchor,
                               thick=thick, v=v))

    # --- geometry -----------------------------------------------------------
    def _geom(self):
        ns = self.nodes
        stages = sorted({n["stage"] for n in ns.values()})
        stage_w = {st: max(n["w"] for n in ns.values() if n["stage"] == st)
                   for st in stages}
        x, cx = {}, self.m_l
        for st in stages:
            x[st] = cx
            cx += stage_w[st] + STAGE
        rows = sorted({n["row"] for n in ns.values()})
        y, cy = {}, M_T
        for r in rows:
            y[r] = cy
            cy += ROW
        self._x, self._y, self._sw = x, y, stage_w
        right = max(x[st] + stage_w[st] / 2 for st in stages) + M_R
        for n in ns.values():
            if n.get("note"):
                right = max(right, x[n["stage"]] + text_w(n["note"], EDGE_FS - 2) / 2 + 24)
        bottom = max(y[r] + H / 2 for r in rows) + M_B
        if self.title:
            right = max(right, text_w(self.title, EDGE_FS) + 2 * self.m_l)
        W = int(round(right))
        Hh = int(round(bottom))
        W += W % 2
        Hh += Hh % 2
        return W, Hh

    def cx(self, nid): return self._x[self.nodes[nid]["stage"]]
    def w(self, nid): return self.nodes[nid]["w"]
    def cy(self, nid): return self._y[self.nodes[nid]["row"]]
    def top(self, nid): return self.cy(nid) - H / 2
    def bottom(self, nid): return self.cy(nid) + H / 2
    def xl(self, nid): return self.cx(nid) - self.w(nid) / 2
    def xr(self, nid): return self.cx(nid) + self.w(nid) / 2

    # --- render -------------------------------------------------------------
    def render(self):
        W, Hh = self._geom()
        p = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {Hh}" '
             f'font-family="{FONT}">']
        p.append("  <defs>")
        def marker(mid, color, size, sw, r):
            s = size
            p.append(f'    <marker id="{mid}" markerWidth="{s*0.95:.1f}" '
                     f'markerHeight="{s*0.95:.1f}" refX="{r}" refY="{s/2:.1f}" '
                     f'orient="auto" markerUnits="userSpaceOnUse">'
                     f'<path d="M2 {s*0.16:.1f} L{s*0.82:.1f} {s/2:.1f} '
                     f'L2 {s*0.84:.1f}" fill="none" stroke="{color}" '
                     f'stroke-width="{sw}" stroke-linecap="round" '
                     f'stroke-linejoin="round"/></marker>')
        for color, tag in [(ARROW, "a"), (G["txt"], "g"), (P["txt"], "p"),
                           (C["txt"], "c"), (INK, "k")]:
            marker("h" + tag, color, 14, 1.9, 11)
            marker("H" + tag, color, 19, 3.0, 15)
        p.append("  </defs>")
        p.append(f'  <rect width="{W}" height="{Hh}" fill="#fff"/>')
        if self.title:
            p.append(f'  <text x="{self.m_l}" y="{M_T - 58}" text-anchor="start" '
                     f'fill="{INK}" font-size="{EDGE_FS}" font-weight="700">'
                     f'{esc(self.title)}</text>')
        lbl = self._label_boxes(W, Hh)
        for i, e in enumerate(self.edges):
            if e["label"]:
                self._elabel(p, e, lbl[i])
        for e in self.edges:
            self._edge(p, e)
        for nid, n in self.nodes.items():
            self._node(p, nid, n)
        p.append("</svg>")
        return "\n".join(p)

    def _mk(self, marker, color):  # arrowhead id for a color
        tag = {"#a1a099": "a", G["txt"]: "g", P["txt"]: "p",
               C["txt"]: "c", INK: "k"}[color]
        return f'url(#{"H" if marker else "h"}{tag})'

    def _node(self, p, nid, n):
        kind = n["kind"]
        cx = self.cx(nid)
        w = self.w(nid)
        x = cx - w / 2
        y = self.top(nid)
        if kind == "dot":
            p.append(f'  <circle cx="{cx:.0f}" cy="{self.cy(nid):.0f}" r="9" '
                     f'fill="{INK}"/>')
            return
        pal = {"work": G, "decide": P, "terminal": C, "deck": G}[kind]
        rx = H / 2 if kind == "terminal" else 0
        fill, line, txt = pal["fill"], pal["line"], pal["txt"]
        if kind == "deck":
            for dx, dy in [(-8, -8), (-16, -16)]:
                p.append(f'  <rect x="{x+dx:.0f}" y="{y+dy:.0f}" '
                         f'width="{w:.0f}" height="{H}" rx="0" fill="{fill}" '
                         f'stroke="{line}" stroke-width="1.2" opacity="0.7"/>')
        p.append(f'  <rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{H}" '
                 f'rx="{rx:.0f}" fill="{fill}" stroke="{line}" stroke-width="2"/>')
        if n.get("sub"):
            p.append(f'  <text x="{cx:.0f}" y="{self.cy(nid)-9:.0f}" '
                     f'text-anchor="middle" fill="{txt}" font-size="{NODE_FS}" '
                     f'font-weight="600">{esc(n["label"])}</text>')
            p.append(f'  <text x="{cx:.0f}" y="{self.cy(nid)+23:.0f}" '
                     f'text-anchor="middle" fill="{INK}" font-size="{EDGE_FS}">'
                     f'{esc(n["sub"])}</text>')
        else:
            p.append(f'  <text x="{cx:.0f}" y="{self.cy(nid)+10:.0f}" '
                     f'text-anchor="middle" fill="{txt}" font-size="{NODE_FS}" '
                     f'font-weight="600">{esc(n["label"])}</text>')
        if n.get("note"):
            p.append(f'  <text x="{cx:.0f}" y="{self.bottom(nid)+36:.0f}" '
                     f'text-anchor="middle" fill="{INK}" font-size="{EDGE_FS-2}">'
                     f'{esc(n["note"])}</text>')

    def _edge(self, p, e):
        a, b = e["a"], e["b"]
        color = e["color"]
        sw = 3 if e["thick"] else 2
        marker = e["thick"]
        dash = ' stroke-dasharray="7 7"' if e["dashed"] else ""
        x1, y1 = self.xr(a), self.cy(a)
        x2, y2 = self.xl(b), self.cy(b)
        below = e.get("below", 0)
        if e.get("v"):
            # downward: exit the bottom-centre of A, enter the top-centre of B
            xa, ya = self.cx(a), self.bottom(a)
            xb, yb = self.cx(b), self.top(b)
            d = (f'M {xa:.0f} {ya:.0f} C {xa:.0f} {ya+26:.0f}, {xb:.0f} {yb-26:.0f}, '
                 f'{xb:.0f} {yb:.0f}')
        elif below:
            d0 = 46 + text_w((e["label"] or ""), EDGE_FS) / 2 + 10
            floor = max(self.bottom(nid) for nid in self.nodes)
            my = floor + below
            d = (f'M {x1:.0f} {y1:.0f} C {x1+d0:.0f} {y1:.0f}, '
                 f'{x1+d0:.0f} {my:.0f}, {x1+d0*.5:.0f} {my:.0f} '
                 f'C {x2-d0:.0f} {my:.0f}, {x2-d0:.0f} {y2:.0f}, '
                 f'{x2:.0f} {y2:.0f}')
        else:
            dx = x2 - x1
            cp = dx * 0.5
            d = (f'M {x1:.0f} {y1:.0f} C {x1+cp:.0f} {y1:.0f}, '
                 f'{x2-cp:.0f} {y2:.0f}, {x2:.0f} {y2:.0f}')
        p.append(f'  <path d="{d}" fill="none" stroke="{color}" '
                 f'stroke-width="{sw}"{dash} marker-end="{self._mk(marker, color)}"/>')

    def _label_boxes(self, W, Hh):
        """Compute every edge label's box; iteratively spread colliding pairs."""
        boxes = []
        for e in self.edges:
            if not e["label"]:
                boxes.append(None)
                continue
            a, b = e["a"], e["b"]
            x1, y1 = self.xr(a), self.cy(a)
            x2, y2 = self.xl(b), self.cy(b)
            if e.get("lp"):
                lp = e["lp"]
                mx, my = lp[0], lp[1]
                anchor = lp[2] if len(lp) > 2 else "middle"
            elif e.get("below"):
                floor = max(self.bottom(nid) for nid in self.nodes)
                mx, my = self.xl(b) - 16, floor + e["below"]
                anchor = "end"
            else:
                mx, my = (x1 + x2) / 2, (y1 + y2) / 2
                anchor = "middle"
            baseline = my - 18
            boxes.append([mx, baseline, text_w(e["label"], EDGE_FS) + 8, 26, anchor])
        for _ in range(60):
            moved = False
            for i in range(len(boxes)):
                if boxes[i] is None:
                    continue
                for j in range(i + 1, len(boxes)):
                    if boxes[j] is None:
                        continue
                    bi, bj = boxes[i], boxes[j]
                    ox = (bi[2] + bj[2]) / 2 - abs(bi[0] - bj[0])
                    oy = (bi[3] + bj[3]) / 2 - abs(bi[1] - bj[1])
                    if ox > 0 and oy > 0:
                        moved = True
                        if ox < oy:
                            sgn = 1 if bi[0] <= bj[0] else -1
                            bi[0] -= sgn * ox / 2
                            bj[0] += sgn * ox / 2
                        else:
                            sgn = 1 if bi[1] <= bj[1] else -1
                            bi[1] -= sgn * oy / 2
                            bj[1] += sgn * oy / 2
            if not moved:
                break
        for box in boxes:
            if box is None:
                continue
            bw, bh = box[2], box[3]
            box[0] = min(max(box[0], bw / 2 + 2), W - bw / 2 - 2)
            box[1] = min(max(box[1], bh / 2 + 2), Hh - bh / 2 - 2)
        return boxes

    def _elabel(self, p, e, box):
        lab = e["label"]
        mx, baseline, bw, bh, anchor = box
        p.append(f'  <text x="{mx:.0f}" y="{baseline:.0f}" text-anchor="{anchor}" '
                 f'fill="{INK}" font-size="{EDGE_FS}">{esc(lab)}</text>')

    # --- verification -------------------------------------------------------
    def verify(self):
        """Crude but real checks: text fits its node; nodes don't collide."""
        problems = []
        W, Hh = self._geom()
        for nid, n in self.nodes.items():
            if n["kind"] in ("work", "decide", "terminal", "deck"):
                tl = text_w(n["label"], NODE_FS)
                if tl + 8 > n["w"]:
                    problems.append(f"{nid}: label too wide for node")
                if n.get("sub"):
                    ts = text_w(n["sub"], EDGE_FS)
                    if ts + 8 > n["w"]:
                        problems.append(f"{nid}: subline too wide for node")
                # nothing may leave the canvas (stage-0 left clip, wide notes)
                x0, y0 = self.xl(nid), self.top(nid)
                x1, y1 = self.xr(nid), self.bottom(nid)
                if n.get("note"):
                    tw = text_w(n["note"], EDGE_FS - 2) / 2
                    x0, x1 = min(x0, self.cx(nid) - tw), max(x1, self.cx(nid) + tw)
                if n["kind"] == "deck":
                    x0, y0 = x0 - 16, y0 - 16
                if x0 < 0 or y0 < 0 or x1 > W or y1 > Hh:
                    problems.append(f"{nid}: clipped by viewBox")
        seen = []
        for nid, n in self.nodes.items():
            box = (self.xl(nid), self.top(nid), self.xr(nid), self.bottom(nid))
            for o in seen:
                if o[0] < box[2] and box[0] < o[2] and o[1] < box[3] and box[1] < o[3]:
                    problems.append(f"overlap {nid} & {o[4]}")
            seen.append((*box, nid))
        # edge labels: use the same resolved positions as the render, check
        # viewBox clipping, node overlap, and label-vs-label collision.
        lboxes = self._label_boxes(W, Hh)
        for i, e in enumerate(self.edges):
            if not e["label"]:
                continue
            box = lboxes[i]
            bw, bh = box[2], box[3]
            lbox = (box[0] - bw / 2, box[1] - bh / 2, box[0] + bw / 2, box[1] + bh / 2)
            if lbox[0] < 0 or lbox[1] < 0 or lbox[2] > W or lbox[3] > Hh:
                problems.append(f"label '{e['label']}' clipped by viewBox")
            for nid in self.nodes:
                if nid in (e["a"], e["b"]):
                    continue
                nbox = (self.xl(nid), self.top(nid), self.xr(nid), self.bottom(nid))
                if (lbox[0] < nbox[2] and nbox[0] < lbox[2]
                        and lbox[1] < nbox[3] and nbox[1] < lbox[3]):
                    problems.append(f"label '{e['label']}' overlaps node {nid}")
        for i in range(len(lboxes)):
            if lboxes[i] is None:
                continue
            for j in range(i + 1, len(lboxes)):
                if lboxes[j] is None:
                    continue
                bi, bj = lboxes[i], lboxes[j]
                li = (bi[0] - bi[2] / 2, bi[1] - bi[3] / 2, bi[0] + bi[2] / 2, bi[1] + bi[3] / 2)
                lj = (bj[0] - bj[2] / 2, bj[1] - bj[3] / 2, bj[0] + bj[2] / 2, bj[1] + bj[3] / 2)
                if (li[0] < lj[2] and lj[0] < li[2]
                        and li[1] < lj[3] and lj[1] < li[3]):
                    problems.append(f"labels '{self.edges[i]['label']}' & "
                                    f"'{self.edges[j]['label']}' overlap")
        return problems

# =============================================================================
# One builder per pattern. Each mirrors the README's "Structure." sentence and
# the shape the older figures drew, tidied onto the shared grid.
# =============================================================================
def mate():
    d = Diagram("Mate-in-One — pick the best fit")
    d.place("job", "job", "terminal", 0, 0)
    d.place("rank", "rank", "decide", 0, 1)
    d.place("worker", "worker", "work", 0, 2)
    d.place("ans", "answer", "terminal", 0, 3)
    d.edge("job", "rank", "one prompt")
    d.edge("rank", "worker", "best fit")
    d.edge("worker", "ans", "a single answer")
    return d

def fanout():
    d = Diagram("Fan-Out — same prompt, N answers, a vote")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("w1", "worker", "work", row=0, stage=2)
    d.place("w2", "worker", "work", row=1, stage=2)
    d.place("w3", "worker", "work", row=2, stage=2)
    d.place("vote", "vote", "decide", row=1, stage=3)
    d.place("exp", "expand", "decide", row=3, stage=3)
    d.place("ans", "answer", "terminal", row=3, stage=4)
    d.edge("job", "dot")
    d.edge("dot", "w1", "same prompt", lp=(256, 74), anchor="middle")
    d.edge("dot", "w2")
    d.edge("dot", "w3")
    d.edge("w1", "vote")
    d.edge("w2", "vote")
    d.edge("w3", "vote")
    d.edge("vote", "ans", "a single answer", lp=None)
    d.edge("vote", "exp", "ties → expand", v=True, lp=(1095, 330, "start"))
    d.edge("exp", "ans")
    return d

def master():
    d = Diagram("Master / Slave — a planner splits the job")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("plan", "planner", "decide", row=1, stage=1)
    d.place("w1", "writer", "work", row=0, stage=2)
    d.place("w2", "thinker", "work", row=2, stage=2)
    d.place("mrg", "merge", "decide", row=1, stage=3)
    d.place("ans", "answer", "terminal", row=1, stage=4)
    d.edge("job", "plan", "plan + split")
    d.edge("plan", "w1", "specialists", lp=(490, 74))
    d.edge("plan", "w2")
    d.edge("w1", "mrg")
    d.edge("w2", "mrg")
    d.edge("mrg", "ans", "a single answer")
    return d

def adversarial():
    d = Diagram("Adversarial — two careful reads, a judge")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("a", "read A", "work", row=0, stage=2)
    d.place("b", "read B", "work", row=2, stage=2)
    d.place("judge", "judge", "decide", row=1, stage=3)
    d.place("ans", "answer", "terminal", row=1, stage=4)
    d.edge("job", "dot")
    d.edge("dot", "a", "careful disagreement", lp=(256, 74))
    d.edge("dot", "b")
    d.edge("a", "judge")
    d.edge("b", "judge")
    d.edge("judge", "ans")
    return d

def strategy():
    d = Diagram("Strategy — each request chooses a pattern")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("ch", "choose plan", "decide", row=1, stage=1)
    d.place("l1", "1 model", "work", row=0, stage=2)
    d.place("l2", "N models", "work", row=1, stage=2)
    d.place("l3", "debate", "work", row=2, stage=2)
    d.place("ans", "answer", "terminal", row=1, stage=3)
    d.edge("job", "ch", "each request")
    d.edge("ch", "l1", "cost × speed × needs", lp=(490, 74))
    d.edge("ch", "l2")
    d.edge("ch", "l3")
    d.edge("l1", "ans")
    d.edge("l2", "ans")
    d.edge("l3", "ans")
    return d

def brute():
    d = Diagram("Brute-Force — many identical tries, keep the best")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("deck", "same rough draft", "deck", row=1, stage=2, sub="N identical",
            note="a stack — many of the same")
    d.place("best", "best", "decide", row=1, stage=3)
    d.place("ans", "answer", "terminal", row=1, stage=4)
    d.edge("job", "dot")
    d.edge("dot", "deck", "same prompt")
    d.edge("deck", "best", "a pick, not a judgment")
    d.edge("best", "ans")
    return d

def verify():
    d = Diagram("Verifier Gate — one draft, a check, retry on fail")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("draft", "draft", "work", row=1, stage=1)
    d.place("check", "check", "decide", row=1, stage=2)
    d.place("ans", "answer", "terminal", row=1, stage=3)
    d.edge("job", "draft", "one try")
    d.edge("draft", "check")
    d.edge("check", "ans", "ok")
    d.edge("check", "draft", "fail → retry", below=40)
    return d

def debate():
    d = Diagram("Debate — two reads that loop until they agree")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("a", "read A", "work", row=0, stage=2)
    d.place("b", "read B", "work", row=2, stage=2)
    d.place("judge", "judge", "decide", row=1, stage=3)
    d.place("ans", "answer", "terminal", row=1, stage=4)
    d.edge("job", "dot")
    d.edge("dot", "a", "careful disagreement", lp=(256, 74))
    d.edge("dot", "b")
    d.edge("a", "judge")
    d.edge("b", "judge")
    d.edge("judge", "ans", "a single answer")
    d.edge("judge", "dot", "disagree → again", dashed=True, below=56)
    return d

def handoff():
    d = Diagram("Pipeline — each step consumes the last")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("s1", "scout", "work", row=1, stage=1)
    d.place("s2", "manifest", "work", row=1, stage=2)
    d.place("s3", "write", "work", row=1, stage=3)
    d.place("s4", "check", "work", row=1, stage=4)
    d.place("ans", "answer", "terminal", row=1, stage=5)
    d.edge("job", "s1")
    d.edge("s1", "s2")
    d.edge("s2", "s3")
    d.edge("s3", "s4")
    d.edge("s4", "ans")
    return d

def ensemble():
    d = Diagram("Ensemble — same prompt, keep the average")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("w1", "worker", "work", row=0, stage=2)
    d.place("w2", "worker", "work", row=1, stage=2)
    d.place("w3", "worker", "work", row=2, stage=2)
    d.place("mean", "mean", "decide", row=1, stage=3)
    d.place("ans", "answer", "terminal", row=1, stage=4)
    d.edge("job", "dot")
    d.edge("dot", "w1", "same prompt", lp=(256, 74))
    d.edge("dot", "w2")
    d.edge("dot", "w3")
    d.edge("w1", "mean")
    d.edge("w2", "mean")
    d.edge("w3", "mean")
    d.edge("mean", "ans", "average the answers")
    return d

def markov():
    d = Diagram("Markowitz Ensemble — correlation-weighted, not just averaged")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("w1", "worker", "work", row=0, stage=2)
    d.place("w2", "worker", "work", row=1, stage=2)
    d.place("w3", "worker", "work", row=2, stage=2)
    d.place("wm", "weighted mean", "decide", row=1, stage=3,
            note="correlation-weighted")
    d.place("ans", "answer", "terminal", row=1, stage=4)
    d.edge("job", "dot")
    d.edge("dot", "w1", "same prompt", lp=(256, 74))
    d.edge("dot", "w2")
    d.edge("dot", "w3")
    d.edge("w1", "wm")
    d.edge("w2", "wm")
    d.edge("w3", "wm")
    d.edge("wm", "ans")
    return d

def negative():
    d = Diagram("Negative Selection — force divergence before you judge")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("w1", "worker", "work", row=0, stage=2)
    d.place("w2", "worker", "work", row=1, stage=2)
    d.place("w3", "worker", "work", row=2, stage=2)
    d.place("sel", "select diverse", "decide", row=1, stage=3,
            note="drop clones, force divergence")
    d.place("vote", "vote", "decide", row=1, stage=4)
    d.place("ans", "answer", "terminal", row=1, stage=5)
    d.edge("job", "dot")
    d.edge("dot", "w1", "same prompt", lp=(256, 74))
    d.edge("dot", "w2")
    d.edge("dot", "w3")
    d.edge("w1", "sel")
    d.edge("w2", "sel")
    d.edge("w3", "sel")
    d.edge("sel", "vote")
    d.edge("vote", "ans")
    return d

def pid():
    d = Diagram("PID Confidence Loop — a budget that tracks error, history, trend")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("set", "set spend", "decide", row=1, stage=1, note="setpoint")
    d.place("smp", "samples", "work", row=1, stage=2)
    d.place("chk", "check", "decide", row=1, stage=3, note="confidence gap")
    d.place("ans", "answer", "terminal", row=1, stage=4)
    d.edge("job", "set")
    d.edge("set", "smp")
    d.edge("smp", "chk")
    d.edge("chk", "ans")
    d.edge("chk", "set", "P · I · D re-command", dashed=True, below=56)
    return d

def pheromone():
    d = Diagram("Pheromone Router — learn which shape wins, with decay")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("ch", "choose", "decide", row=1, stage=1, note="learned weights")
    d.place("l1", "1 model", "work", row=0, stage=2)
    d.place("l2", "N models", "work", row=1, stage=2)
    d.place("l3", "debate", "work", row=2, stage=2)
    d.place("ans", "answer", "terminal", row=1, stage=3)
    d.edge("job", "ch")
    d.edge("ch", "l1", "learned weights", lp=(470, 74))
    d.edge("ch", "l2")
    d.edge("ch", "l3")
    d.edge("l1", "ans")
    d.edge("l2", "ans")
    d.edge("l3", "ans")
    d.edge("ans", "ch", "verified → reinforce, others decay", dashed=True, below=56)
    return d

def byzantine():
    d = Diagram("Byzantine Adjudicator — dose spend by the shape of doubt")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("w1", "worker", "work", row=0, stage=2)
    d.place("w2", "worker", "work", row=1, stage=2)
    d.place("w3", "worker", "work", row=2, stage=2)
    d.place("cls", "classify", "decide", row=1, stage=3,
            note="noise vs byzantine split")
    d.place("ans", "answer", "terminal", row=1, stage=4)
    d.edge("job", "dot")
    d.edge("dot", "w1", "same prompt", lp=(256, 74))
    d.edge("dot", "w2")
    d.edge("dot", "w3")
    d.edge("w1", "cls")
    d.edge("w2", "cls")
    d.edge("w3", "cls")
    d.edge("cls", "ans")
    return d

def straggler():
    d = Diagram("Straggler Backup — duplicate only the overdue worker")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("worker", "worker", "work", row=1, stage=1)
    d.place("ans", "answer", "terminal", row=1, stage=2)
    d.place("back", "backup", "work", row=2, stage=1, note="over budget → spawn")
    d.edge("job", "worker", "one try")
    d.edge("worker", "ans", "a single answer")
    d.edge("worker", "back", "overdue", dashed=True, v=True)
    d.edge("back", "ans", "first to finish", lp=(560, 156))
    return d

def materialized():
    d = Diagram("Materialized Answer — cache the verified answer by a semantic key")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("hash", "hash", "decide", row=1, stage=1, note="semantic key")
    d.place("pat", "pattern", "work", row=3, stage=2)
    d.place("ans", "answer", "terminal", row=1, stage=3)
    d.edge("job", "hash")
    d.edge("hash", "ans", "hit → short-circuits", dashed=True)
    d.edge("hash", "pat", "miss → compute")
    d.edge("pat", "ans", "verified → cache")
    return d

def canary():
    d = Diagram("Canary Trust-Equity — earn a vote before you ever cast one")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("inc", "incumbent", "work", row=0, stage=1)
    d.place("can", "canary", "work", row=2, stage=1)
    d.place("cmp", "compare", "decide", row=1, stage=2)
    d.place("gate", "gate", "decide", row=1, stage=3)
    d.place("ans", "answer", "terminal", row=1, stage=4)
    d.edge("job", "inc", "same request")
    d.edge("job", "can", "shadow", dashed=True)
    d.edge("inc", "cmp")
    d.edge("can", "cmp")
    d.edge("cmp", "gate", "shadow → compare → gate")
    d.edge("gate", "ans", "earns a vote")
    return d

def cvar():
    d = Diagram("CVaR Budgeting — size the spend by the tail, not the mean")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("rank", "rank", "decide", row=1, stage=1, note="consequential?")
    d.place("vote", "vote", "work", row=0, stage=2, note="mean")
    d.place("div", "diverge", "work", row=2, stage=2, note="expected-shortfall")
    d.place("ans", "answer", "terminal", row=1, stage=3)
    d.edge("job", "rank")
    d.edge("rank", "vote", "pick the tail-safe shape", lp=(470, 74))
    d.edge("rank", "div")
    d.edge("vote", "ans")
    d.edge("div", "ans")
    return d

def circuit():
    d = Diagram("Circuit Breaker + Bulkhead — fail fast, quarantine the toxic class")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("w1", "worker", "work", row=0, stage=2)
    d.place("w2", "worker", "work", row=1, stage=2)
    d.place("w3", "worker", "work", row=2, stage=2)
    d.place("brk", "breaker", "decide", row=1, stage=3, note="open / half-open / closed")
    d.place("pool", "pool", "decide", row=1, stage=4, note="quarantine the toxic class")
    d.place("ans", "answer", "terminal", row=1, stage=5)
    d.edge("job", "dot")
    d.edge("dot", "w1", "same prompt", lp=(256, 74))
    d.edge("dot", "w2")
    d.edge("dot", "w3")
    d.edge("w1", "brk")
    d.edge("w2", "brk")
    d.edge("w3", "brk")
    d.edge("brk", "pool")
    d.edge("pool", "ans")
    return d

def delphi():
    d = Diagram("Delphi Consensus — anonymous numeric rounds until the spread closes")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("e1", "estimate", "work", row=0, stage=2)
    d.place("e2", "estimate", "work", row=1, stage=2)
    d.place("e3", "estimate", "work", row=2, stage=2)
    d.place("agg", "aggregate", "decide", row=1, stage=3, note="median + rationales")
    d.place("ans", "answer", "terminal", row=1, stage=4)
    d.edge("job", "dot")
    d.edge("dot", "e1", "anonymous, N models", lp=(256, 74))
    d.edge("dot", "e2")
    d.edge("dot", "e3")
    d.edge("e1", "agg")
    d.edge("e2", "agg")
    d.edge("e3", "agg")
    d.edge("agg", "ans", "convergence")
    d.edge("agg", "dot", "revise → R rounds", dashed=True, below=56)
    return d

def trial_seq():
    d = Diagram("Trial Sequential Analysis — the learner only wins once N is met")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("ch", "challenger", "work", row=0, stage=1)
    d.place("inc", "incumbent", "work", row=2, stage=1)
    d.place("nv", "N verified", "decide", row=1, stage=2,
            note="N set a priori, not by streak")
    d.place("prom", "promote?", "decide", row=1, stage=3, note="wider each look")
    d.place("best", "best", "terminal", row=1, stage=4)
    d.edge("job", "ch", "same class")
    d.edge("job", "inc", lp=(330, 74))
    d.edge("ch", "nv")
    d.edge("inc", "nv")
    d.edge("nv", "prom")
    d.edge("prom", "best", "only at the threshold")
    return d

def evid_bar():
    d = Diagram("Evidence-Bar Ladder — proof threshold scales with the cost of error")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("coc", "class of cost", "decide", row=1, stage=1, note="which error is worse?")
    d.place("p1", "pass", "decide", row=0, stage=2, note="preponderance")
    d.place("p2", "checks", "decide", row=1, stage=2, note="clear-and-convincing")
    d.place("p3", "divide", "decide", row=2, stage=2, note="beyond-reasonable-doubt")
    d.place("ans", "answer", "terminal", row=1, stage=3)
    d.edge("job", "coc")
    d.edge("coc", "p1")
    d.edge("coc", "p2")
    d.edge("coc", "p3")
    d.edge("p1", "ans")
    d.edge("p2", "ans")
    d.edge("p3", "ans")
    return d

def screening():
    d = Diagram("Type-Revelation Screening — probe a model's type in idle, before trust")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("probe", "probe bank", "decide", row=0, stage=1)
    d.place("model", "model", "work", row=2, stage=1)
    d.place("upd", "update prior", "decide", row=1, stage=2, note="hit rate → type bucket")
    d.place("alloc", "allocate", "decide", row=1, stage=3, note="sort before real work")
    d.place("ans", "answer", "terminal", row=1, stage=4)
    d.edge("job", "probe", "idle, off critical path", lp=(330, 74))
    d.edge("job", "model", lp=(330, 420))
    d.edge("probe", "upd")
    d.edge("model", "upd")
    d.edge("upd", "alloc")
    d.edge("alloc", "ans")
    return d

def condorcet():
    d = Diagram("Condorcet Pairwise Pooling — head-to-head beats plurality")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("dot", "", "dot", row=1, stage=1)
    d.place("a1", "answer", "work", row=0, stage=2)
    d.place("a2", "answer", "work", row=1, stage=2)
    d.place("a3", "answer", "work", row=2, stage=2)
    d.place("pw", "pairwise", "decide", row=1, stage=3, note="beats every rival")
    d.place("best", "best", "terminal", row=1, stage=4)
    d.edge("job", "dot")
    d.edge("dot", "a1", "3+ camps", lp=(256, 74))
    d.edge("dot", "a2")
    d.edge("dot", "a3")
    d.edge("a1", "pw")
    d.edge("a2", "pw")
    d.edge("a3", "pw")
    d.edge("pw", "best", "Condorcet, not plurality")
    return d

def slack_steal():
    d = Diagram("Slack-Stealing Scheduler — background only in the idle")
    d.place("job", "job", "terminal", row=0, stage=0)
    d.place("live", "live request", "work", row=0, stage=1, note="deadline-bearing")
    d.place("ans", "answer", "terminal", row=0, stage=2)
    d.place("idle", "idle task", "decide", row=2, stage=1, note="only in the slack")
    d.place("pre", "preempt", "decide", row=2, stage=2, note="yields on arrival")
    d.edge("job", "live")
    d.edge("live", "ans")
    d.edge("idle", "pre")
    d.edge("pre", "live", "yields", thick=True, below=0)
    return d

def thompson():
    d = Diagram("Thompson Posterior Router — route by sampling each posterior")
    d.place("job", "job", "terminal", row=1, stage=0)
    d.place("pa", "A posterior", "decide", row=0, stage=1, note="per-arm Beta")
    d.place("pb", "B posterior", "decide", row=1, stage=1)
    d.place("pc", "C posterior", "decide", row=2, stage=1)
    d.place("smp", "sample", "decide", row=1, stage=2, note="highest sample wins")
    d.place("ans", "answer", "terminal", row=1, stage=3)
    d.edge("job", "pa")
    d.edge("job", "pb")
    d.edge("job", "pc")
    d.edge("pa", "smp")
    d.edge("pb", "smp")
    d.edge("pc", "smp")
    d.edge("smp", "ans", "verified wins feed back", dashed=True, below=0)
    return d

BUILDERS = {
    "mate": mate, "fanout": fanout, "master": master, "adversarial": adversarial,
    "strategy": strategy, "brute": brute, "verify": verify, "debate": debate,
    "handoff": handoff, "ensemble": ensemble, "markov": markov,
    "negative": negative, "pid": pid, "pheromone": pheromone,
    "byzantine": byzantine, "straggler": straggler, "materialized": materialized,
    "canary": canary, "cvar": cvar, "circuit": circuit, "delphi": delphi,
    "trial_seq": trial_seq, "evid_bar": evid_bar, "screening": screening,
    "condorcet": condorcet, "slack_steal": slack_steal, "thompson": thompson,
}

# index gallery title lines: name / tagline (from the one-sentence table)
INDEX_ROWS = [
    ("Mate-in-One", "pick the best fit"),
    ("Fan-Out", "same prompt, N answers, a vote"),
    ("Master / Slave", "a planner splits the job"),
    ("Adversarial", "two careful reads, a judge"),
    ("Strategy", "each request chooses a pattern"),
    ("Brute-Force", "many identical tries, keep the best"),
    ("Verifier Gate", "one draft, a check, retry on fail"),
    ("Debate", "two reads that loop until they agree"),
    ("Pipeline", "each step consumes the last"),
    ("Ensemble", "same prompt, keep the average"),
    ("Markowitz Ensemble", "correlation-weighted, not just averaged"),
    ("Negative Selection", "force divergence before you judge"),
    ("PID Confidence Loop", "a budget that tracks error, history, trend"),
    ("Pheromone Router", "learn which shape wins, with decay"),
    ("Byzantine Adjudicator", "dose spend by the shape of doubt"),
    ("Straggler Backup", "duplicate only the overdue worker"),
    ("Materialized Answer", "cache the verified answer by a semantic key"),
    ("Canary Trust-Equity", "earn a vote before you ever cast one"),
    ("CVaR Budgeting", "size the spend by the tail, not the mean"),
    ("Circuit Breaker + Bulkhead", "fail fast, quarantine the toxic class"),
    ("Delphi Consensus", "anonymous numeric rounds until agreement"),
    ("Trial Sequential Analysis", "the learner only wins once N is met"),
    ("Evidence-Bar Ladder", "proof threshold scales with the cost of error"),
    ("Type-Revelation Screening", "probe a model's type in idle, before trust"),
    ("Condorcet Pairwise Pooling", "head-to-head beats plurality"),
    ("Slack-Stealing Scheduler", "background only in the idle a live request leaves"),
    ("Thompson Posterior Router", "route by sampling each posterior, not argmax"),
]

def build_index():
    """Compose every pattern figure onto one tall canvas."""
    svgs = []
    gap = 90
    cursor = 60
    for name, fn in BUILDERS.items():
        svgs.append((name, fn().render()))
    # parse each svg's viewBox to place it
    import re
    body = []
    cursor = 40
    for name, svg in svgs:
        m = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        h = int(m.group(2))
        body.append(f'<g transform="translate(30,{cursor})">')
        body.append(_strip(svg))
        body.append('</g>')
        end = cursor + h
        # title block to the left handled inside strip; advance
        cursor = end + gap
    total_h = cursor + 40
    widths = []
    for _, svg in svgs:
        m2 = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        widths.append(int(m2.group(1)))
    total_w = max(widths) + 2 * 40
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w} {total_h}" '
           f'font-family="{FONT}">']
    out.append(f'<rect width="{total_w}" height="{total_h}" fill="#fff"/>')
    out.extend(body)
    out.append('</svg>')
    return "\n".join(out)

def _strip(svg):
    """Extract the inner content of a generated <svg>, dropping its outer tag."""
    i = svg.index(">") + 1
    j = svg.rindex("</svg>")
    return svg[i:j]

def main():
    os.makedirs(OUT, exist_ok=True)
    import re
    problems = {}
    for name, fn in BUILDERS.items():
        d = fn()
        probs = d.verify()
        svg = d.render()
        with open(os.path.join(OUT, name + ".svg"), "w") as f:
            f.write(svg)
        w = re.search(r'viewBox="0 0 (\d+) (\d+)"', svg)
        problems[name] = probs
        print(f"wrote {name}.svg  [{w.group(1)}x{w.group(2)}]  ver{'ok' if not probs else 'PROBLEMS: '+str(probs)}")
    idx = build_index()
    with open(os.path.join(OUT, "index.svg"), "w") as f:
        f.write(idx)
    print("wrote index.svg")
    print("done")

if __name__ == "__main__":
    main()
