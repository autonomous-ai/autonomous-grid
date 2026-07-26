# The figure style

Every figure in this repo is drawn by `figs.py` and assembled by `build_figures.py`. There are no
hand-made images and no diagramming tool, which is the point: a figure is code, so it can be
reviewed, diffed, and corrected when the thing it describes changes.

This file is the style. If a new figure needs something not written here, add it here first.

---

## 1. Colour

Six values, sampled from the PNGs in Anthropic's engineering posts rather than guessed. Three
families, each a pale fill, a mid-tone line and a dark text of the same hue — so a shape reads as
tinted paper with ink on it, never as a block of colour.

| | fill | line | text | means |
|---|---|---|---|---|
| **green** | `#eff5ea` | `#b8d7a6` | `#5a912f` | things you already own — machines, your data |
| **purple** | `#f3f2f9` | `#a9a3c9` | `#5d579d` | the intelligence layer — the grid, the trainer, models |
| **coral** | `#fbf0ed` | `#f0c5b6` | `#ad5130` | the human edges — what goes in, what comes out, people |

| | value | used for |
|---|---|---|
| **arrow** | `#a1a099` | every line and arrowhead. A warm grey, never black |
| **text** | `#2b2b29` | edge labels and anything not inside a shape |
| **page** | `#ffffff` | the background, always, on every figure |

**The colours carry meaning, not decoration.** A reader who has seen two figures should be able to
guess what a green box is in the third. Never introduce a fourth hue to distinguish two things that
the existing three already separate; if two things need separating and share a family, separate them
by shape or position instead.

**Arrows are grey, not black.** This is the single easiest rule to break and the one that costs the
most. Black lines pull ahead of the shapes and the figure starts to read as a diagram of arrows.
The grey sits behind the labels and lets them lead.

## 2. Type

```
Avenir Next, Nunito, Segoe UI, Helvetica, Arial, sans-serif
```

| role | size | weight |
|---|---|---|
| node label, inside a shape | 25 | 600 |
| edge label, on or beside a line | 22 | 400 |
| flywheel station | 35 | 700 |
| flywheel feeder station | 21 | 700 |

Node labels are **short**: one or two words, three at the outside. This is the rule that does most
of the work in the whole system — anything longer than a label goes on the arrow, where there is
room for it. A node whose text has to wrap is a node that is doing two jobs.

## 3. Shape

| shape | what it is |
|---|---|
| **box**, square corners | a stage that does something — a machine, a model, a step |
| **stadium**, fully rounded ends | a terminal — what enters the system or leaves it |
| **band**, a box stretched along an axis | a span of time; its width means hours |
| **bare text**, no shape at all | a station on a flywheel |

Every box, stadium and band is **66px tall** with a **2px** border. One height for everything is
what makes a row of unlike things read as one row.

A flywheel is the exception that proves it: its stations carry no shape at all, because a box around
each one turns a wheel into six separate objects and the circle stops being the subject.

## 4. Line

| | width | meaning |
|---|---|---|
| edge | 2 | the ordinary flow — a thing moves from here to there |
| dashed edge | 2 | a return, a repeat, or many parallel copies of one thing |
| rim | 3 | the closed loop of a flywheel, heavier so a ring reads as one wheel |

Arrowheads are open chevrons, never filled triangles, and they come in one size per line width. Each
one is drawn in its line's own colour — an SVG marker carries its own fill, so a coloured line with
the shared grey head ends in a grey tip.

**Curve vocabulary.** A straight line where nothing is in the way. A single bend where two things sit
at different heights. An arc where several lines leave one point and must stay apart — the curve
alone separates them, and no two lines in one figure should cross if a bend would avoid it.

## 5. Layout

- **The drawing decides the page.** A figure computes its own extent and the canvas grows to fit it;
  no figure is fitted into a fixed frame.
- **A gap is as wide as the label that crosses it.** Edge labels are never placed over a shape, and
  the space between two nodes grows to make room for the words on the arrow between them.
- **Arcs stop clear of what they touch,** by that node's own width, so a wide label never has a line
  running underneath it.
- **Nothing is added to help the reader.** No legend, no key, no colour scale, no title inside the
  frame. The README's prose is the caption. A legend is a sign that the drawing has not done its job.

## 6. Output

`python3 docs/build_figures.py` writes every `.svg`. The READMEs embed the `.png` beside it, and
**nothing regenerates those automatically** — render at **2× device scale** on the SVG's own viewBox
and overwrite the PNG by hand. A figure changed and not re-rendered keeps showing the old picture
with nothing failing, which has happened.

Every figure carries an `alt` describing what it shows in the order a reader would read it. These
are long on purpose; the figure is often the clearest statement of an idea on the page, and a reader
who cannot see it should get the idea rather than its title.
