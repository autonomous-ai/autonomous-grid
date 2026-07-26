"""Draw the training figures in the register Anthropic's engineering posts use.

Every value here was measured off the article's own PNGs rather than guessed:
fills, borders, text colours, the warm grey of the arrows, the 66px node height
that every shape shares, and the square corners. The one rule that does most of
the work is that a node carries a single short label — anything else goes on the
arrow.
"""
import pathlib

# ---- tokens, sampled from cdn.sanity.io/.../building-effective-agents PNGs ----
GREEN_FILL, GREEN_LINE, GREEN_TEXT = "#eff5ea", "#b8d7a6", "#5a912f"
PURPLE_FILL, PURPLE_LINE, PURPLE_TEXT = "#f3f2f9", "#a9a3c9", "#5d579d"
CORAL_FILL, CORAL_LINE, CORAL_TEXT = "#fbf0ed", "#f0c5b6", "#ad5130"
ARROW, LABEL_TEXT = "#a1a099", "#2b2b29"

H = 66            # every node is this tall
BORDER = 2
STROKE = 2
NODE_FS = 25      # node label
LABEL_FS = 22     # edge label
PAD = 22          # horizontal padding inside a box

FONT = "Avenir Next, Nunito, Segoe UI, Helvetica, Arial, sans-serif"

NARROW = set("iljtfr1.,'! ")
WIDE = set("mwMW")


def text_w(s, fs=NODE_FS):
    u = fs / 25.0
    return sum((8 if c in NARROW else 24 if c in WIDE else 15.5) for c in s) * u


def box_w(label):
    return round(text_w(label) + 2 * PAD)


class Fig:
    def __init__(self, w, h):
        self.w, self.h, self.parts = w, h, []
        self.maxx = self.maxy = 0

    def _saw(self, x, y):
        self.maxx = max(self.maxx, x)
        self.maxy = max(self.maxy, y)

    # ---- shapes -------------------------------------------------------
    def box(self, cx, cy, label, kind="green", w=None):
        fill, line, text = {
            "green": (GREEN_FILL, GREEN_LINE, GREEN_TEXT),
            "purple": (PURPLE_FILL, PURPLE_LINE, PURPLE_TEXT),
        }[kind]
        w = w or box_w(label)
        x, y = cx - w / 2, cy - H / 2
        self.parts.append(
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{H}" '
            f'fill="{fill}" stroke="{line}" stroke-width="{BORDER}"/>')
        self.parts.append(
            f'<text x="{cx:.0f}" y="{cy + 9:.0f}" text-anchor="middle" fill="{text}" '
            f'font-size="{NODE_FS}" font-weight="600">{label}</text>')
        self._saw(cx + w / 2, cy + H / 2)
        return w

    def term(self, cx, cy, label, w=None):
        """A terminal — circle when the label is short, stadium when it is not."""
        w = w or max(H + 8, box_w(label) - 8)
        x, y = cx - w / 2, cy - H / 2
        self.parts.append(
            f'<rect x="{x:.0f}" y="{y:.0f}" width="{w:.0f}" height="{H}" rx="{H/2:.0f}" '
            f'fill="{CORAL_FILL}" stroke="{CORAL_LINE}" stroke-width="{BORDER}"/>')
        self.parts.append(
            f'<text x="{cx:.0f}" y="{cy + 9:.0f}" text-anchor="middle" fill="{CORAL_TEXT}" '
            f'font-size="{NODE_FS}" font-weight="600">{label}</text>')
        self._saw(cx + w / 2, cy + H / 2)
        return w

    # ---- edges --------------------------------------------------------
    def arrow(self, x1, y1, x2, y2, dashed=False, curve=0):
        d = (f"M{x1:.0f} {y1:.0f} C{x1 + curve:.0f} {y1:.0f}, {x2 - curve:.0f} {y2:.0f}, "
             f"{x2:.0f} {y2:.0f}") if curve else f"M{x1:.0f} {y1:.0f} L{x2:.0f} {y2:.0f}"
        dash = ' stroke-dasharray="7 7"' if dashed else ""
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{ARROW}" stroke-width="{STROKE}"{dash} '
            f'marker-end="url(#h)"/>')
        self._saw(max(x1, x2), max(y1, y2))

    def elbow(self, pts, dashed=False):
        d = "M" + " L".join(f"{x:.0f} {y:.0f}" for x, y in pts)
        dash = ' stroke-dasharray="7 7"' if dashed else ""
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{ARROW}" stroke-width="{STROKE}"{dash} '
            f'marker-end="url(#h)"/>')
        for x, y in pts:
            self._saw(x, y)

    def label(self, cx, cy, *lines, anchor="middle"):
        for i, ln in enumerate(lines):
            self.parts.append(
                f'<text x="{cx:.0f}" y="{cy + i * 28:.0f}" text-anchor="{anchor}" '
                f'fill="{LABEL_TEXT}" font-size="{LABEL_FS}">{ln}</text>')
            self._saw(cx + text_w(ln, LABEL_FS) / 2, cy + i * 28)

    # ---- output -------------------------------------------------------
    def svg(self):
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
            f'font-family="{FONT}">\n'
            f'  <defs>\n'
            f'    <marker id="h" markerWidth="14" markerHeight="14" refX="11" refY="5" '
            f'orient="auto" markerUnits="userSpaceOnUse">\n'
            f'      <path d="M1.5 1 L10 5 L1.5 9" fill="none" stroke="{ARROW}" '
            f'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>\n'
            f'    </marker>\n'
            f'  </defs>\n'
            f'  <rect width="{self.w}" height="{self.h}" fill="#fff"/>\n  ')
        return head + "\n  ".join(self.parts) + "\n</svg>\n"

    def write(self, path):
        # the drawing decides the page, not the other way round
        self.w = max(self.w, int(self.maxx) + 100)
        self.h = max(self.h, int(self.maxy) + 60)
        pathlib.Path(path).write_text(self.svg())
        return path
