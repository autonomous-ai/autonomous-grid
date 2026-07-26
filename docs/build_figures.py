"""Generate the six training figures. One system, six drawings."""
import os

from figs import (
    ARROW,
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
# The trainer DRIVES. It holds the tasks and the weights and it opens every
# request; the orchestrator places that request on whichever machine is free.
# Drawn left to right in that order, because the old version read as a pipeline
# ending at the trainer, which is the opposite of how the loop runs.
f = Fig(1580, 730)
y = 300
tw2 = box_w("The trainer")
tcx = M + tw2 / 2
f.box(tcx, y, "The trainer", kind="purple", w=tw2)

gw = box_w("The orchestrator")
gcx = tcx + tw2 / 2 + 330 + gw / 2
f.box(gcx, y, "The orchestrator", kind="purple", w=gw)
f.arrow(tcx + tw2 / 2 + 12, y, gcx - gw / 2 - 14, y)
f.label((tcx + tw2 / 2 + gcx - gw / 2) / 2, y - 26, "one task, eight attempts")

MW = 220
mcx = gcx + gw / 2 + GAP + MW / 2
rows = [y - 120, y, y + 120]
for ry, name in zip(rows, ["MacBook Pro", "Mac Studio", "RTX box"]):
    f.box(mcx, ry, name, w=MW)
    curve = 0 if ry == y else 64
    off = 14 if ry > y else -14 if ry < y else 0
    f.arrow(gcx + gw / 2 + 12, y + off, mcx - MW / 2 - 14, ry, dashed=True, curve=curve)


# What comes back is not just text: the ids the engine actually sampled, which is
# the behaviour-policy term of the loss and the reason a chat API will not do.
f.label(mcx, rows[2] + H / 2 + 46, "placed on whichever machine is free")
f.elbow([(mcx + MW / 2 + 12, rows[2]), (mcx + MW / 2 + 128, rows[2]),
         (mcx + MW / 2 + 128, 604), (tcx, 604), (tcx, y + H / 2 + 14)])
f.label((tcx + mcx) / 2, 648, "completions, and the token ids they sampled")

# The adapter does not go back through the orchestrator — the trainer pushes it
# straight to the machines that sample.
f.elbow([(tcx, y - H / 2 - 12), (tcx, 96), (mcx, 96), (mcx, rows[0] - H / 2 - 14)], dashed=True)
f.label((tcx + mcx) / 2, 62, "the adapter, straight to every machine, every two steps")
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
f = Fig(1700, 1180)
CX, CY, R = 800, 680, 400
INK = ARROW   # grey, never black — docs/STYLE.md section 1

# Nouns, not quantities. The reference says Sellers, Selection, Traffic — never
# "more sellers" — because a flywheel already means "more of this drives more of
# that", and repeating it in every label is the diagram explaining its own form.
# Colour carries the same meaning it carries everywhere else in the set: green is
# what you already own, purple is the intelligence layer, coral is the human edge.
f.wheel(CX, CY, R, [
    ("Models", PURPLE_TEXT),
    ("Superhuman work", CORAL_TEXT),
    ("Employees", CORAL_TEXT),
    ("Computers", GREEN_TEXT),
], hub_r=240, hub_lines=("DOMAIN-SPECIFIC", "INTELLIGENCE"), fs=CORE_FS, start=0,
   arc=INK)

# The second loop leaves from Employees — they are the ones who connect a system —
# and rejoins the wheel at Models, because your own models ARE models. The words
# "domain-specific" appear once, in the hub: three times and the drawing looks
# like it knows one adjective.
# What it bypasses is Computers: this is the way to more models that needs nobody
# to buy hardware.
#
# Two things about the drawing. The branch and the rim leave as two separate
# arrows with air between them rather than from a shared dot: a junction reads as
# a knot at this line weight. And the whole loop is held a clear band away from
# the rim, because two curves running close together read as one thick curve.
f.block(258, 200, ("Your systems",), GREEN_TEXT, CORE_FS)
# Named rather than badged. Real logos would be the only thing on any of these
# seven figures that is not drawn by this file — an external asset to license,
# keep current and re-export — and the names carry the meaning on their own.
f.block(258, 252, ("Gmail · Slack · Notion", "Drive · Salesforce"),
        LABEL_TEXT, SAT_FS, weight=400)
f.block(800, 118, ("Your data",), GREEN_TEXT, CORE_FS)
f.block(1396, 230, ("Your models",), PURPLE_TEXT, CORE_FS)

f.bow(336, 604, 274, 322, lift=-18, colour=INK, width=RIM)
f.bow(400, 168, 668, 132, lift=-24, colour=INK, width=RIM)
f.bow(936, 132, 1252, 190, lift=-28, colour=INK, width=RIM)
f.bow(1470, 292, 1256, 610, lift=-46, colour=INK, width=RIM)

# The one place a verb earns its keep: Computers is the only station that does two
# different jobs, and without this second arrow the drawing shows only the first.
# The same machines serve by day and train at night — section 4. The two arrows
# leave Computers well apart: the rim departs at about 288 degrees, so this one
# starts clear above it rather than from the same few pixels.
f.bow(922, 236, 1240, 258, lift=-20, colour=INK, width=RIM)
f.label(1074, 292, "train")
f.label(1042, 442, "serve")

f.write(f"{OUT}/fig-flywheel.svg")

print("wrote seven figures")
# NOTE: this writes SVGs only. The README embeds the PNGs beside them, and nothing
# here regenerates those — a redrawn figure keeps showing the old picture on the
# page, with nothing failing. Converting on mtime does not work, because every run
# rewrites every SVG; it would have to compare content, and only for the figures
# this file actually owns (fig-sutton, home-grid and train-grid come from elsewhere).
