# Diagrams

Every figure in this repo is generated from one token set, so they read as one family and
cannot drift apart.

```
python3 docs/build_figures.py     # writes the six SVGs
```

Then render each SVG to a PNG at 1.5×:

```
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless --disable-gpu \
  --hide-scrollbars --force-device-scale-factor=1.5 --window-size=<viewBox w>,<viewBox h> \
  --screenshot=docs/<name>.png "file://$PWD/docs/<name>.svg"
```

**The READMEs embed the PNGs, not the SVGs.** An updated SVG that was never re-rendered changes
nothing on the page — this has already cost us a day, with three figures redrawn but invisible.
Re-render, look at the result, then commit.

## The two files

- `figs.py` — the tokens and the shapes. Colours here were sampled from Anthropic's
  *Building Effective Agents* diagrams, not picked by eye. Change a value here and every figure
  follows.
- `build_figures.py` — the six drawings. Each is a few lines: a chain of nodes, some branches,
  a feedback path.

## The rules

The full standard is `autonomous-org/knowledge/diagram-style.md`. The short version:

- A node carries **one short label**. Everything else goes on the arrow.
- Green does work, purple decides, coral is a terminal. No colour for emphasis.
- Solid = the main path. Dashed = delegated, optional, or looping back.
- Square corners, one node height, white background, no shadows.
- The gap an arrow crosses is sized to its caption, so a label never lands on a box.

## The exception

`fig-sutton.svg` is deliberately **not** in this style. It reproduces Figure 3.1 of Sutton &
Barto, so it keeps the book's own register and carries an attribution line. Our version of the
same loop sits directly beneath it in house style — the contrast is the point.

`home-grid.svg` predates the standard and still uses the older panel style with product logos
baked in (see `build_diagram.py`, which inlines them). Not yet converted.
