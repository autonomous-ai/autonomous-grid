"""Generate the six training figures. One system, six drawings."""
import os

from figs import Fig, box_w, H

OUT = os.path.dirname(os.path.abspath(__file__))
M = 100          # page margin
GAP = 120        # the gap an arrow crosses


def chain(f, y, items, edges=None):
    """Lay a row of nodes left to right, drawing the arrows between them.

    `edges` is one entry per gap: None, a string, or a tuple of lines. The gap
    grows to fit its label so a caption can never sit on top of a box — the
    thing that was wrong with every earlier version of these figures.
    """
    from figs import text_w, LABEL_FS
    edges = edges or [None] * (len(items) - 1)
    widths = [rest[0] if rest else (box_w(label) - 8 if kind == "term" else box_w(label))
              for kind, label, *rest in items]
    gaps = []
    for e in edges:
        if e is None:
            gaps.append(GAP)
        else:
            lines = (e,) if isinstance(e, str) else e
            gaps.append(max(GAP, max(text_w(ln, LABEL_FS) for ln in lines) + 66))
    x, placed = M, []
    for i, ((kind, label, *_), w) in enumerate(zip(items, widths)):
        cx = x + w / 2
        (f.term if kind == "term" else f.box)(
            cx, y, label, **({} if kind == "term" else {"kind": kind}), w=w)
        placed.append((cx, w))
        if i < len(gaps):
            x += w + gaps[i]
        else:
            x += w
    for i, e in enumerate(edges):
        (cx1, w1), (cx2, w2) = placed[i], placed[i + 1]
        f.arrow(cx1 + w1 / 2 + 12, y, cx2 - w2 / 2 - 14, y)
        if e is not None:
            lines = (e,) if isinstance(e, str) else e
            top = y - 24 - 28 * (len(lines) - 1)
            f.label((cx1 + w1 / 2 + cx2 - w2 / 2) / 2, top, *lines)
    return placed


# ---------------------------------------------------------------- fig-loop
f = Fig(920, 330)
y = 130
(task, tw), (model, mw), (check, cw) = chain(
    f, y, [("term", "A task"), ("green", "Your model"), ("purple", "A check")],
    edges=[None, "an attempt"])
f.elbow([(check, y + H / 2 + 12), (check, 246), (model, 246), (model, y + H / 2 + 14)], dashed=True)
f.label((model + check) / 2, 288, "a reward between 0 and 1")
f.write(f"{OUT}/fig-loop.svg")

# ---------------------------------------------------------------- fig-step
f = Fig(1620, 400)
y = 190
(task, tw), (att, aw), (avg, vw) = chain(
    f, y, [("term", "One task"), ("green", "Eight attempts"), ("purple", "Their average")],
    edges=[None, "scored against"])
OW = 190
up, dn = y - 100, y + 100
ocx = avg + vw / 2 + GAP + OW / 2
f.box(ocx, up, "Likelier", w=OW)
f.box(ocx, dn, "Less likely", w=OW)
f.arrow(avg + vw / 2 + 12, y - 14, ocx - OW / 2 - 14, up, curve=70)
f.arrow(avg + vw / 2 + 12, y + 14, ocx - OW / 2 - 14, dn, curve=70)
f.label(avg + vw / 2 + 52, up - 26, "above it")
f.label(avg + vw / 2 + 52, dn + 40, "below it")
sw = box_w("One step") - 8
scx = ocx + OW / 2 + GAP + sw / 2
f.term(scx, y, "One step", w=sw)
f.arrow(ocx + OW / 2 + 12, up, scx - sw / 2 - 14, y - 14, curve=70)
f.arrow(ocx + OW / 2 + 12, dn, scx - sw / 2 - 14, y + 14, curve=70)
f.write(f"{OUT}/fig-step.svg")

# --------------------------------------------------------------- fig-fleet
f = Fig(1560, 470)
y = 200
(tasks, tw), (grid, gw) = chain(f, y, [("term", "Tasks"), ("purple", "The grid")])
MW = 220
mcx = grid + gw / 2 + GAP + MW / 2
rows = [y - 110, y, y + 110]
for ry, name in zip(rows, ["MacBook Pro", "Mac Studio", "RTX box"]):
    f.box(mcx, ry, name, w=MW)
trw = box_w("The trainer")
tcx = mcx + MW / 2 + GAP + trw / 2
f.box(tcx, y, "The trainer", kind="purple", w=trw)
for ry in rows:
    curve = 0 if ry == y else 60
    f.arrow(grid + gw / 2 + 12, y + (14 if ry > y else -14 if ry < y else 0),
            mcx - MW / 2 - 14, ry, dashed=True, curve=curve)
    f.arrow(mcx + MW / 2 + 12, ry, tcx - trw / 2 - 14,
            y + (14 if ry > y else -14 if ry < y else 0), dashed=True, curve=curve)
f.label(mcx, rows[0] - 60, "one task each, eight attempts")
aw2 = box_w("One adapter") - 8
acx = tcx + trw / 2 + GAP + aw2 / 2
f.term(acx, y, "One adapter", w=aw2)
f.arrow(tcx + trw / 2 + 12, y, acx - aw2 / 2 - 14, y)
f.elbow([(tcx, y + H / 2 + 12), (tcx, 404), (grid, 404), (grid, y + H / 2 + 14)], dashed=True)
f.label((grid + tcx) / 2, 444, "back to the machines, every two steps")
f.write(f"{OUT}/fig-fleet.svg")

# ----------------------------------------------------------- fig-day-night
f = Fig(1700, 440)
y = 200
(day, dw), (kept, kw), (climb, cw), (gate, gw) = chain(
    f, y, [("term", "The workday"), ("green", "Every answer kept"),
           ("green", "The climb"), ("purple", "The gate")],
    edges=[None, ("what happened,", "attached in the evening"), "23:00"])
ow = box_w("Served") - 8
ocx = gate + gw / 2 + GAP + ow / 2
f.term(ocx, y - 100, "Served", w=ow)
f.term(ocx, y + 100, "Binned", w=ow)
f.arrow(gate + gw / 2 + 12, y - 14, ocx - ow / 2 - 14, y - 100, curve=60)
f.arrow(gate + gw / 2 + 12, y + 14, ocx - ow / 2 - 14, y + 100, dashed=True, curve=60)
f.label(gate + gw / 2 + 66, y - 118, "better")
f.label(gate + gw / 2 + 74, y + 152, "not better")
turn = ocx + ow / 2 + 60          # clear of the pill, or the line runs through it
f.elbow([(ocx + ow / 2 + 8, y - 100), (turn, y - 100), (turn, 380), (day, 380),
         (day, y + H / 2 + 14)], dashed=True)
f.label(760, 414, "tomorrow, on a better model")
f.write(f"{OUT}/fig-day-night.svg")

# --------------------------------------------------------------- fig-earns
f = Fig(900, 500)
cy = 250
tw = box_w("One answer") - 8
tcx = M + tw / 2
f.term(tcx, cy, "One answer", w=tw)
BW = 260
bcx = tcx + tw / 2 + 200 + BW / 2
rows = [(cy - 165, "You rewrote it", "1.0", False),
        (cy - 55, "A stronger model", "0.8", False),
        (cy + 55, "You sent it as-is", "0.6", False),
        (cy + 165, "You binned it", "never imitated", True)]
for ry, label, weight, dashed in rows:
    if dashed:
        f.term(bcx, ry, label, w=BW)
    else:
        f.box(bcx, ry, label, w=BW)
    # every arrow leaves the same point; the curve alone separates them, as in
    # the article's parallelisation figure
    f.arrow(tcx + tw / 2 + 12, cy, bcx - BW / 2 - 14, ry, dashed=dashed, curve=110)
    # the weight sits against its own row, never floating between two of them
    f.label(bcx - BW / 2 - 46, ry + 8, weight, anchor="end")
f.write(f"{OUT}/fig-earns.svg")

# ------------------------------------------------------- train-architecture
f = Fig(1680, 430)
y = 190
(work, ww), (mach, mw), (train, tw2), (gate, gw) = chain(
    f, y, [("term", "Your work"), ("green", "Machines you own"),
           ("purple", "One trainer"), ("purple", "The gate")],
    edges=[None, "eight attempts a task", "an adapter"])
ow = box_w("Served") - 8
ocx = gate + gw / 2 + GAP + ow / 2
f.term(ocx, y - 100, "Served", w=ow)
f.term(ocx, y + 100, "Binned", w=ow)
f.arrow(gate + gw / 2 + 12, y - 14, ocx - ow / 2 - 14, y - 100, curve=60)
f.arrow(gate + gw / 2 + 12, y + 14, ocx - ow / 2 - 14, y + 100, dashed=True, curve=60)
f.label(gate + gw / 2 + 66, y - 118, "better")
f.label(gate + gw / 2 + 74, y + 152, "not better")
f.elbow([(train, y + H / 2 + 12), (train, 350), (mach, 350), (mach, y + H / 2 + 14)],
        dashed=True)
f.label((mach + train) / 2, 390, "the new adapter, every two steps")
f.write(f"{OUT}/train-architecture.svg")

print("wrote six figures")
