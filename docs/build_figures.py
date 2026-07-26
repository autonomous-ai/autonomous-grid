"""Generate the six training figures. One system, six drawings."""
import os

from figs import (  # noqa
    CORAL_TEXT,
    CORE_FS,
    GREEN_TEXT,
    LABEL_TEXT,
    PURPLE_TEXT,
    RIM,
    SAT_FS,
    Fig,
    H,
    box_w,
)

OUT = os.path.dirname(os.path.abspath(__file__))
M = 100          # page margin
GAP = 120        # the gap an arrow crosses


def chain(f, y, items, edges=None):
    """Lay a row of nodes left to right, drawing the arrows between them.

    `edges` is one entry per gap: None, a string, or a tuple of lines. The gap
    grows to fit its label so a caption can never sit on top of a box — the
    thing that was wrong with every earlier version of these figures.
    """
    from figs import LABEL_FS, text_w
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
# One job only: show that the same machines run two shifts. The gate, the climb
# and what gets served are explained by the other figures — not again here.
f = Fig(1600, 400)
X0, X1 = 100, 1500
PER_HOUR = (X1 - X0) / 24
at = lambda hour: X0 + ((hour - 9) % 24) * PER_HOUR   # the day starts at 09:00
band_y = 140
f.band(X0, at(17) - 6, band_y, "Serving")
f.band(at(17) + 6, X1, band_y, "Training", kind="purple")
f.label((X0 + at(17)) / 2, band_y - 56, "day shift")
f.label((at(17) + X1) / 2, band_y - 56, "night shift")
f.label((X0 + at(17)) / 2, band_y + 74, "people work, every answer kept")
f.label((at(17) + X1) / 2, band_y + 74, "idle machines, the model gets better")
f.label((X0 + at(17)) / 2, band_y + 106, "8 hours")
f.label((at(17) + X1) / 2, band_y + 106, "16 hours")
f.axis(X0, X1, 300, [(X0, "09:00"), (at(17), "17:00"), (at(0), "00:00"), (X1, "09:00")])
f.arrow(X1, 390, X0, 390, dashed=True)
f.label((X0 + X1) / 2, 428, "and again tomorrow, on a better model")
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
# The machines are drawn as a fan of three, not as one box saying "machines you
# own". This is the first figure on the page and the claim it has to land is that
# the work spreads across whatever you already have — a single box says "a
# server", which is the opposite of the point, and the fan says it without a
# caption. Named machines rather than generic ones, and the same three the fleet
# figure uses later, so the second time a reader meets them they recognise them.
f = Fig(1900, 520)
y = 210
ww = box_w("Your work") - 8
wcx = M + ww / 2
f.term(wcx, y, "Your work", w=ww)

MW = 236
mcx = wcx + ww / 2 + 150 + MW / 2
rows = [y - 110, y, y + 110]
for ry, name in zip(rows, ["MacBook Pro", "Mac Studio", "RTX box"]):
    f.box(mcx, ry, name, w=MW)

tw2 = box_w("One trainer")
tcx = mcx + MW / 2 + 300 + tw2 / 2
f.box(tcx, y, "One trainer", kind="purple", w=tw2)

# Out of one point and into one point, the curve alone separating the three —
# the same treatment the fleet figure uses, so the two read as one system.
for ry in rows:
    curve = 0 if ry == y else 62
    off = 14 if ry > y else -14 if ry < y else 0
    f.arrow(wcx + ww / 2 + 12, y + off, mcx - MW / 2 - 14, ry, curve=curve)
    f.arrow(mcx + MW / 2 + 12, ry, tcx - tw2 / 2 - 14, y + off, curve=curve)
# over the gap it describes — the fan-IN carries the attempts, not the fan-out
f.label((mcx + MW / 2 + tcx - tw2 / 2) / 2, rows[0] - 62, "eight attempts a task")

gw = box_w("The gate")
gcx = tcx + tw2 / 2 + 210 + gw / 2
f.box(gcx, y, "The gate", kind="purple", w=gw)
f.arrow(tcx + tw2 / 2 + 12, y, gcx - gw / 2 - 14, y)
f.label((tcx + tw2 / 2 + gcx - gw / 2) / 2, y - 24, "an adapter")

ow = box_w("Served") - 8
ocx = gcx + gw / 2 + GAP + ow / 2
f.term(ocx, y - 110, "Served", w=ow)
f.term(ocx, y + 110, "Binned", w=ow)
f.arrow(gcx + gw / 2 + 12, y - 14, ocx - ow / 2 - 14, y - 110, curve=60)
f.arrow(gcx + gw / 2 + 12, y + 14, ocx - ow / 2 - 14, y + 110, dashed=True, curve=60)
f.label(gcx + gw / 2 + 66, y - 128, "better")
f.label(gcx + gw / 2 + 74, y + 162, "not better")

# The adapter goes back to EVERY machine, and the single return line can only
# touch one box, so the caption carries what the geometry cannot.
f.elbow([(tcx, y + H / 2 + 12), (tcx, 430), (mcx, 430), (mcx, rows[2] + H / 2 + 14)],
        dashed=True)
f.label((mcx + tcx) / 2, 470, "the new adapter, to every machine, every two steps")
f.write(f"{OUT}/train-architecture.svg")

# -------------------------------------------------------------- fig-flywheel
# Three loops, and inference is what they all turn on — which is why it is the
# hub rather than a station. Grid is an inference layer first; the training plane
# is built on top of it, so the drawing should say that before it says anything
# else.
#
#   compute        (green)  people arrive with machines, so capacity grows with
#                           headcount rather than with the bill
#   training       (purple) inference produces the work that gets judged, trained
#                           on, and served back cheaper — needs no new hardware
#   specialisation (coral)  data for one job earns a model for that job, which
#                           pulls more of that job in; it compounds PER JOB TYPE
#
# Amazon's grammar rather than Doordash's: a tight core ring turning around the
# hub, with a larger loop departing and rejoining it. Two rings, so the inner one
# is rotated — two rings both starting at the top put a station directly above a
# station and the joining arrow then runs through both labels.
f = Fig(2200, 1370)
CX, CY = 1000, 760
R1, R2 = 340, 600

# --- the core: compute. Turns with headcount, and a cloud product cannot copy it
f.wheel(CX, CY, R1, [
    ("More people", GREEN_TEXT),
    ("More compute", GREEN_TEXT),
    ("More intelligence", GREEN_TEXT),
], hub_r=142, hub_lines=("MORE", "INFERENCE"), fs=32, start=0, arc=GREEN_TEXT)

# --- the second loop: training. Departs the hub and rejoins it, needing nothing
#     new once work is already flowing
f.wheel(CX, CY, R2, [
    ("More connectors", PURPLE_TEXT),
    ("More data", PURPLE_TEXT),
    ("More training", PURPLE_TEXT),
    ("Better models", PURPLE_TEXT),
    ("Lower cost per task", PURPLE_TEXT),
], fs=32, arc=PURPLE_TEXT)

# The two joins that make it one wheel rather than two drawings: inference is
# what gets connected to, and cheaper tasks are what come back as more inference.
f.bow(CX, CY - 156, CX, 214, lift=0, colour=PURPLE_TEXT, width=RIM)
f.bow(556, 616, CX - 150, CY - 48, lift=0, colour=PURPLE_TEXT, width=RIM)

# --- the third loop: specialisation. Hung off More data, because that is where a
#     job's own examples accumulate.
f.block(1930, 388, ("One model", "per job"), CORAL_TEXT, SAT_FS)
f.block(1946, 726, ("More of that job", "routed to it"), CORAL_TEXT, SAT_FS)
f.bow(1660, 540, 1852, 412, lift=26, colour=CORAL_TEXT)
f.bow(1938, 452, 1944, 668, lift=-40, colour=CORAL_TEXT)
f.bow(1852, 760, 1664, 620, lift=26, colour=CORAL_TEXT)

f.key(120, 1146, [
    (GREEN_TEXT, "Compute — turns with headcount"),
    (PURPLE_TEXT, "Training — turns with usage"),
    (CORAL_TEXT, "Specialisation — turns per job"),
])

f.write(f"{OUT}/fig-flywheel.svg")

print("wrote seven figures")
# NOTE: this writes SVGs only. The README embeds the PNGs beside them, and nothing
# here regenerates those — a redrawn figure keeps showing the old picture on the
# page, with nothing failing. Converting on mtime does not work, because every run
# rewrites every SVG; it would have to compare content, and only for the figures
# this file actually owns (fig-sutton, home-grid and train-grid come from elsewhere).
