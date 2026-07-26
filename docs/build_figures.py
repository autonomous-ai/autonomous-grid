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
# Amazon's flywheel, station for station. Two loops, because theirs is two, and
# the reason theirs is memorable is that it is small enough to hold in one look.
#
# Four things make the lines read the way the reference's do, and all four are
# structural rather than decorative:
#   1. the second loop FORKS off the wheel at a marked point — it does not cross
#      it. Two lines leaving one dot read as a branch; two lines meeting in open
#      space read as a mistake.
#   2. the centre is large against the ring, a little over half its diameter, so
#      the wheel reads as a disc with words round it rather than a hoop.
#   3. the lines are near-black and heavy. Thin grey is what this system uses for
#      annotation; this figure is the argument.
#   4. nothing else is on the page — no legend, no colour coding.
#
#   growth -> intelligence            sellers -> more computers
#   selection -> more models          traffic -> more employees
#   customer experience -> superhuman work
#   the second loop: connectors -> domain-specific data -> domain-specific models
f = Fig(1700, 1190)
CX, CY, R = 800, 660, 430
INK = LABEL_TEXT

f.wheel(CX, CY, R, [
    ("More models", LABEL_TEXT),
    ("Superhuman work", LABEL_TEXT),
    ("More employees", LABEL_TEXT),
    ("More computers", LABEL_TEXT),
], hub_r=250, hub_lines=("INTELLIGENCE",), fs=CORE_FS, arc=INK)

# The second loop forks off the rim rather than leaving the hub, so it crosses
# nothing, and it lands back on the station it exists to improve. Connecting a
# system is what turns a general model into one that knows your work.
f.dot(448, 413)
f.block(258, 244, ("More connectors",), LABEL_TEXT, CORE_FS)
f.block(806, 96, ("More domain-", "specific data"), LABEL_TEXT, CORE_FS)
f.block(1338, 252, ("More domain-", "specific models"), LABEL_TEXT, CORE_FS)

f.bow(448, 413, 336, 290, lift=-26, colour=INK, width=RIM)
f.bow(432, 214, 616, 126, lift=-30, colour=INK, width=RIM)
f.bow(1000, 118, 1136, 196, lift=-26, colour=INK, width=RIM)
f.bow(1428, 336, 1280, 602, lift=-44, colour=INK, width=RIM)

f.write(f"{OUT}/fig-flywheel.svg")

print("wrote seven figures")
# NOTE: this writes SVGs only. The README embeds the PNGs beside them, and nothing
# here regenerates those — a redrawn figure keeps showing the old picture on the
# page, with nothing failing. Converting on mtime does not work, because every run
# rewrites every SVG; it would have to compare content, and only for the figures
# this file actually owns (fig-sutton, home-grid and train-grid come from elsewhere).
