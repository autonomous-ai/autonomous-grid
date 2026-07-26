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
MW = 270         # a machine box; one width, so any two fleet rows line up
FGAP = 26        # between machines: close enough that the row reads as one strip

# One fleet, named and ordered once. Three figures drew this row and all three
# disagreed — two called the same box "RTX box" where a third said "RTX 6000",
# and home-grid ordered it differently again. A reader who meets these machines
# on the front page should recognise the same five, in the same order, in every
# later figure; three spellings of one fleet reads as three different fleets.
# Engine, memory and served model belong here too, so the roll-up on the front
# page can be DERIVED rather than typed — a hand-typed total is a number that
# goes stale the first time this tuple changes.
FLEET = (
    # name           engine     GB   the model it serves
    ("MacBook Pro",  "MLX",      64, "Qwen3-30B-A3B"),
    ("Mac Studio",   "MLX",     256, "MiniMax-M2"),
    ("Mac mini",     "Ollama",   24, "Gemma 3 12B"),
    ("RTX 6000",     "vLLM",     48, "Qwen3-32B"),
    ("RTX 5090",     "vLLM",     32, "Gemma 3 27B"),
)
NAMES = tuple(name for name, *_ in FLEET)


def fleet_row(f, page_w, y, detail=False):
    """Draw the fleet centred on the page. Returns each box's centre x.

    `detail` hangs the engine, the memory and the served model under each box.
    Only the front page wants them: there the fleet IS the subject, and a bare
    row of product names does not say what makes one machine different from the
    next. Inside the training figures the same row is scenery for an argument
    about placement, and three extra lines per box would drown it.
    """
    span = len(FLEET) * MW + (len(FLEET) - 1) * FGAP
    x0 = (page_w - span) / 2
    xs = [x0 + MW / 2 + i * (MW + FGAP) for i in range(len(FLEET))]
    for cx, (name, engine, gb, model) in zip(xs, FLEET):
        f.box(cx, y, name, w=MW)
        if detail:
            f.label(cx, y + H / 2 + 38, f"{engine} · {gb} GB", model)
    return xs


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
        draw = f.term if kind == "term" else f.stack if kind == "stack" else f.box
        draw(cx, y, label, **({} if kind == "term" else {"kind": kind.replace("stack", "green")}), w=w)
        placed.append((cx, w, kind))
        if i < len(gaps):
            x += w + gaps[i]
        else:
            x += w
    for i, e in enumerate(edges):
        (cx1, w1, k1), (cx2, w2, _) = placed[i], placed[i + 1]
        # a stack's back cards sit up and to the right, so its edge is further out
        lead = 12 + (20 if k1 == "stack" else 0)
        f.arrow(cx1 + w1 / 2 + lead, y, cx2 - w2 / 2 - 14, y)
        if e is not None:
            lines = (e,) if isinstance(e, str) else e
            top = y - 24 - 28 * (len(lines) - 1)
            f.label((cx1 + w1 / 2 + cx2 - w2 / 2) / 2, top, *lines)
    return [(cx, w) for cx, w, _ in placed]


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
    f, y, [("term", "One task"), ("stack", "Several attempts"), ("purple", "Their average")],
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
f.label((tcx + tw2 / 2 + gcx - gw / 2) / 2, y - 26, "one task, several attempts")

mcx = gcx + gw / 2 + GAP + MW / 2
# Three of the five, not all five: this figure is about one task landing on a
# free machine, and five rows would make the fleet the subject instead. Names
# come from FLEET so it is visibly a subset, never a different set.
rows = [y - 120, y, y + 120]
for ry, name in zip(rows, [NAMES[0], NAMES[1], NAMES[3]]):
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
# One job only: the same machines run two shifts. Day is pale and night is filled
# — the only solid block in the set, because here the contrast IS the subject and
# it says what two captions used to. "day shift"/"night shift" came out with it.
f = Fig(1600, 430)
X0, X1 = 100, 1500
PER_HOUR = (X1 - X0) / 24
at = lambda hour: X0 + ((hour - 9) % 24) * PER_HOUR   # the day starts at 09:00
band_y = 132
f.band(X0, at(17) - 6, band_y, "Inference")
f.band(at(17) + 6, X1, band_y, "Training", kind="night")
f.label((X0 + X1) / 2, band_y - 60, "the same machines, two shifts")
f.label((X0 + at(17)) / 2, band_y + 76, "8 hours · people work, every answer kept")
f.label((at(17) + X1) / 2, band_y + 76, "16 hours · idle machines, the model gets better")
f.axis(X0, X1, 268, [(X0, "09:00"), (at(17), "17:00"), (at(0), "00:00"), (X1, "09:00")])
f.arrow(X1, 356, X0, 356, dashed=True)
f.label((X0 + X1) / 2, 394, "and again tomorrow, on a better model")
f.write(f"{OUT}/fig-day-night.svg")

# --------------------------------------------------------------- fig-earns
f = Fig(900, 500)
cy = 250
tw = box_w("One answer") - 8
tcx = M + tw / 2
f.term(tcx, cy, "One answer", w=tw)
BW = 260
bcx = tcx + tw / 2 + 200 + BW / 2
# The weights are train/capture.py's, not illustrative: edited 1.0, teacher 0.8,
# accepted 0.6, discarded stored but never used as a target.
rows = [(cy - 165, "You rewrote it", "counts 1.0", False),
        (cy - 55, "A stronger model", "counts 0.8", False),
        (cy + 55, "You sent it as-is", "counts 0.6", False),
        (cy + 165, "You binned it", "never imitated", True)]
for i, (ry, label, weight, dashed) in enumerate(rows):
    if dashed:
        f.term(bcx, ry, label, w=BW)
    else:
        f.box(bcx, ry, label, w=BW)
    # Each leaves its own point down the stadium's edge. All four from one pixel
    # made a knot at the origin that read as an arrowhead pointing back INTO the
    # answer — the exact opposite of the direction the figure means.
    f.arrow(tcx + tw / 2 + 12, cy - 18 + i * 12, bcx - BW / 2 - 14, ry,
            dashed=dashed, curve=110)
    # Past the boxes, not before them. Four arrows converge on the left edge and
    # a number parked in that convergence is unreadable at any size; to the right
    # the four stack into a column that scans on its own, and "counts" on each
    # row means the column needs no header to say what it measures.
    f.label(bcx + BW / 2 + 40, ry + 8, weight, anchor="start")
f.write(f"{OUT}/fig-earns.svg")

# ------------------------------------------------------- train-architecture
# The first figure on the page, so the direction has to be right: the trainer
# holds your tasks and the weights and drives everything. One line across the top
# is the story — tasks in, adapter out, gate decides. The fleet sits underneath
# the trainer because it is not a stage in that line, it is what the trainer
# reaches for and gets answers back from.
f = Fig(1700, 880)
y = 250
ww = box_w("Your tasks") - 8
wcx = M + ww / 2
f.term(wcx, y, "Your tasks", w=ww)

tw2 = box_w("The trainer")
tcx = wcx + ww / 2 + 150 + tw2 / 2
f.box(tcx, y, "The trainer", kind="purple", w=tw2)
f.arrow(wcx + ww / 2 + 12, y, tcx - tw2 / 2 - 14, y)

gw = box_w("The gate")
gcx = tcx + tw2 / 2 + 430 + gw / 2
f.box(gcx, y, "The gate", kind="purple", w=gw)
f.arrow(tcx + tw2 / 2 + 12, y, gcx - gw / 2 - 14, y)
f.label((tcx + tw2 / 2 + gcx - gw / 2) / 2, y - 26, "an adapter")

ow = box_w("Served") - 8
ocx = gcx + gw / 2 + 190 + ow / 2
f.term(ocx, y - 110, "Served", w=ow)
f.term(ocx, y + 110, "Binned", w=ow)
f.arrow(gcx + gw / 2 + 12, y - 14, ocx - ow / 2 - 14, y - 110, curve=60)
f.arrow(gcx + gw / 2 + 12, y + 14, ocx - ow / 2 - 14, y + 110, dashed=True, curve=60)
f.label(gcx + gw / 2 + 62, y - 128, "better")
f.label(gcx + gw / 2 + 70, y + 162, "not better")

# The fleet, under the trainer, reached THROUGH the orchestrator — the trainer
# never talks to a machine directly, it asks the grid and the grid places the
# work. The fleet figure says the same thing; these two must not disagree.
ocw = box_w("The orchestrator")
oy = 470
f.box(tcx, oy, "The orchestrator", kind="purple", w=ocw)
my = 720
# Five, spread the full width: the fleet is the point of the page and a huddle of
# three under the trainer left half the frame empty saying nothing.
fleet_x = fleet_row(f, 1700, my)

# Both directions live in the gap between the trainer and the orchestrator,
# stacked directly above one another. The trainer never speaks to a machine: it
# asks the grid and the grid answers, so the round trip belongs on that one hop.
f.arrow(tcx - 64, y + H / 2 + 12, tcx - 64, oy - H / 2 - 14)
f.label(tcx - 96, 352, "a task,", "several attempts", anchor="end")
f.arrow(tcx + 64, oy - H / 2 - 14, tcx + 64, y + H / 2 + 12)
f.label(tcx + 96, 340, "the attempts, and", "the token ids they sampled", anchor="start")

# Each arrow leaves its own point along the orchestrator's edge. Five lines from
# one pixel is a knot; five from a spread edge is a distribution, which is what
# this actually is.
for i, bx in enumerate(fleet_x):
    f.arrow(tcx - 120 + i * 60, oy + H / 2 + 12, bx, my - H / 2 - 14, dashed=True)
f.label(1700 - M, my + H / 2 + 56, "placed on whichever machine is free", anchor="end")
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

# "Productivity", not "Superhuman ability": every other station on this wheel is
# a concrete noun, and a claim sits badly among them. It is also what a buyer
# actually calls this. Not "Employee productivity" — Employees is the very next
# station, so the adjacency already says whose.
#
# Nouns, not quantities. The reference says Sellers, Selection, Traffic — never
# "more sellers" — because a flywheel already means "more of this drives more of
# that", and repeating it in every label is the diagram explaining its own form.
# Colour carries the same meaning it carries everywhere else in the set: green is
# what you already own, purple is the intelligence layer, coral is the human edge.
f.wheel(CX, CY, R, [
    ("Models", PURPLE_TEXT),
    ("Productivity", CORAL_TEXT),
    ("Employees", CORAL_TEXT),
    ("Computers", GREEN_TEXT),
], hub_r=240, hub_lines=("COMPOUNDING", "INTELLIGENCE"), fs=CORE_FS, start=0,
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
# "Expert models", not "domain-specific models": every other station on this wheel
# is a word a reader already owns, and that was the last piece of jargon. It also
# draws the contrast harder — experts joining the plain Models on the rim.
f.block(1424, 226, ("Expert models",), PURPLE_TEXT, CORE_FS)

f.bow(336, 604, 274, 322, lift=-18, colour=INK, width=RIM)
f.label(356, 470, "connect")
f.bow(400, 168, 668, 132, lift=-24, colour=INK, width=RIM)
f.bow(936, 132, 1256, 192, lift=-28, colour=INK, width=RIM)
f.bow(1508, 288, 1256, 610, lift=-54, colour=INK, width=RIM)

# The one place a verb earns its keep: Computers is the only station that does two
# different jobs, and without this second arrow the drawing shows only the first.
# The same machines serve by day and train at night — section 4. The two arrows
# leave Computers well apart: the rim departs at about 288 degrees, so this one
# starts clear above it rather than from the same few pixels.
f.bow(922, 236, 1250, 258, lift=-20, colour=INK, width=RIM)
f.label(1074, 292, "train")
f.label(1042, 442, "serve")

f.write(f"{OUT}/fig-flywheel.svg")

# ------------------------------------------------------------ fig-inference
# The serving half, drawn in the same shape as the training half so the two read
# as one system: one line across the top, the fleet underneath the thing that
# places work on it. Simpler than training because there is no gate and no
# adapter — a request goes out, an answer comes back.
f = Fig(1700, 500)
y = 152
aw3 = box_w("Your apps") - 8
acx = M + aw3 / 2
f.term(acx, y, "Your apps", w=aw3)
f.label(acx, y + 66, "OpenClaw · Hermes", "your own code")

# Parked over the fourth machine, not the middle one. Centring it left the whole
# top-right of the page empty, and the machine that answers is whichever one was
# free — an off-centre pick says that better than a symmetric one.
ocw2 = box_w("The orchestrator")
occx = (1700 - (len(FLEET) * MW + (len(FLEET) - 1) * FGAP)) / 2 + MW / 2 + 3 * (MW + FGAP)
f.box(occx, y, "The orchestrator", kind="purple", w=ocw2)
f.arrow(acx + aw3 / 2 + 12, y - 18, occx - ocw2 / 2 - 14, y - 18)
f.label((acx + aw3 / 2 + occx - ocw2 / 2) / 2, y - 40, "one OpenAI-compatible endpoint")
f.arrow(occx - ocw2 / 2 - 14, y + 18, acx + aw3 / 2 + 12, y + 18)
f.label((acx + aw3 / 2 + occx - ocw2 / 2) / 2, y + 58, "the answer")

# The same five machines as the training figure, at the same width and spacing,
# so the two rows stack when a reader scrolls from one section to the next.
# ONE pair of arrows, into the middle box: serving a request lights up a single
# machine, and four boxes sitting quietly either side is what says so. Training
# fans to all five because training uses all five — that contrast is the whole
# difference between the two halves, and it only reads if the rows are identical.
my2 = 402
fleet_row(f, 1700, my2)
f.arrow(occx - 64, y + H / 2 + 12, occx - 64, my2 - H / 2 - 14)
f.arrow(occx + 64, my2 - H / 2 - 14, occx + 64, y + H / 2 + 12)
f.label(occx - 96, 262, "whichever computer", "serves that model", anchor="end")
f.write(f"{OUT}/fig-inference.svg")

# ---------------------------------------------------------------- home-grid
# The front-page picture: what talks to the grid, what the grid is, and what it
# runs on. The grid is one box with two halves rather than two boxes, because it
# is one address — serving and training are jobs it does, not peers of it.
f = Fig(1660, 790)
APPS = ["Grid Desktop", "OpenClaw", "Hermes", "Your own app"]
aw4 = max(box_w(a) for a in APPS) - 8
span = len(APPS) * aw4 + (len(APPS) - 1) * 56
ax0 = (1660 - span) / 2
app_y = 96
app_x = []
for i, a in enumerate(APPS):
    cx = ax0 + aw4 / 2 + i * (aw4 + 56)
    f.term(cx, app_y, a, w=aw4)
    app_x.append(cx)

gy = 372
PH = 240
# Derived, never typed: these three go stale the moment FLEET changes otherwise.
STATS = [f"{len(FLEET)} nodes",
         f"{len({model for *_, model in FLEET})} models",
         f"{sum(gb for _, _, gb, _ in FLEET)} GB GPU memory"]
f.panel(M, 1660 - M, gy, "Your local AI grid", STATS, ["Inference", "Training"], h=PH)
for cx in app_x:
    f.arrow(cx, gy - PH / 2 - 12, cx, app_y + H / 2 + 14)

mach_y = 650
for cx in fleet_row(f, 1660, mach_y, detail=True):
    f.arrow(cx, mach_y - H / 2 - 12, cx, gy + PH / 2 + 14)
f.label(1660 / 2, mach_y + H / 2 + 128, "each machine keeps the engine it already runs")
f.write(f"{OUT}/home-grid.svg")

print("wrote nine figures")
# NOTE: this writes SVGs only. The README embeds the PNGs beside them, and nothing
# here regenerates those — a redrawn figure keeps showing the old picture on the
# page, with nothing failing. Converting on mtime does not work, because every run
# rewrites every SVG; it would have to compare content, and only for the figures
# this file actually owns (fig-sutton, home-grid and train-grid come from elsewhere).
