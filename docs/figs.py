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
RIM = 3           # the flywheel rim — heavier than an edge so a ring reads as one wheel
CORE_FS = 35      # a station on the driving loop — the only text at this size
SAT_FS = 21       # a station on a feeder loop, subordinate to the core by size and colour
NODE_FS = 28      # node label
LABEL_FS = 24     # edge label
PAD = 22          # horizontal padding inside a box

FONT = "Avenir Next, Nunito, Segoe UI, Helvetica, Arial, sans-serif"

# An SVG marker carries its own colour, so a coloured arc drawn with the grey
# arrowhead ends in a grey tip. One marker pair per colour the drawings use.
MARKERS = {ARROW: "a", GREEN_TEXT: "g", PURPLE_TEXT: "p", CORAL_TEXT: "c",
           LABEL_TEXT: "k"}


def head(colour=None, heavy=False):
    """The marker id for an arrow of this colour and weight."""
    return ("H" if heavy else "h") + MARKERS.get(colour or ARROW, "a")

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

    def stack(self, cx, cy, label, kind="green", w=None, depth=2, off=9):
        """A node that stands for several of the same thing.

        Drawn as a deck rather than as N labelled boxes: the count is a knob, and
        naming one would assert a number the code does not derive. What matters is
        that there is more than one and they are peers — which is the whole reason
        GRPO can use their average as its baseline.
        """
        fill, line, _ = {
            "green": (GREEN_FILL, GREEN_LINE, GREEN_TEXT),
            "purple": (PURPLE_FILL, PURPLE_LINE, PURPLE_TEXT),
        }[kind]
        w = w or box_w(label)
        for i in range(depth, 0, -1):
            self.parts.append(
                f'<rect x="{cx - w / 2 + i * off:.0f}" y="{cy - H / 2 - i * off:.0f}" '
                f'width="{w:.0f}" height="{H}" fill="{fill}" stroke="{line}" '
                f'stroke-width="{BORDER}"/>')
        self.box(cx, cy, label, kind=kind, w=w)
        self._saw(cx + w / 2 + depth * off, cy + H / 2)
        return w

    def band(self, x1, x2, cy, label, kind="green"):
        """A stretch of time. Same tokens as a node; the width carries the hours.

        `night` is the one filled block in the whole set, and it is not decoration:
        the figure's subject IS day against night, so the contrast does work no
        caption can. Nothing else may use it.
        """
        fill, line, text = {
            "green": (GREEN_FILL, GREEN_LINE, GREEN_TEXT),
            "purple": (PURPLE_FILL, PURPLE_LINE, PURPLE_TEXT),
            "night": (PURPLE_TEXT, PURPLE_TEXT, "#ffffff"),
        }[kind]
        self.parts.append(
            f'<rect x="{x1:.0f}" y="{cy - H / 2:.0f}" width="{x2 - x1:.0f}" height="{H}" '
            f'fill="{fill}" stroke="{line}" stroke-width="{BORDER}"/>')
        self.parts.append(
            f'<text x="{(x1 + x2) / 2:.0f}" y="{cy + 9:.0f}" text-anchor="middle" fill="{text}" '
            f'font-size="{NODE_FS}" font-weight="600">{label}</text>')
        self._saw(x2, cy + H / 2)

    def panel(self, x1, x2, cy, title, halves, h=176):
        """One thing with two halves inside it.

        The only container in the set, and it earns being one: the grid is a
        single address, and serving and training are two jobs it does — drawing
        them as three peers would say there are three things.
        """
        self.parts.append(
            f'<rect x="{x1:.0f}" y="{cy - h / 2:.0f}" width="{x2 - x1:.0f}" height="{h}" '
            f'fill="{PURPLE_FILL}" stroke="{PURPLE_LINE}" stroke-width="{BORDER}"/>')
        self.parts.append(
            f'<text x="{(x1 + x2) / 2:.0f}" y="{cy - h / 2 + 42:.0f}" text-anchor="middle" '
            f'fill="{PURPLE_TEXT}" font-size="{NODE_FS}" font-weight="700">{title}</text>')
        iw = max(box_w(t) for t in halves) + 96
        gap = 44
        left = (x1 + x2) / 2 - (len(halves) * iw + (len(halves) - 1) * gap) / 2
        for i, t in enumerate(halves):
            cx = left + iw / 2 + i * (iw + gap)
            self.parts.append(
                f'<rect x="{cx - iw / 2:.0f}" y="{cy + 6:.0f}" width="{iw:.0f}" height="{H}" '
                f'fill="#fff" stroke="{PURPLE_LINE}" stroke-width="{BORDER}"/>')
            self.parts.append(
                f'<text x="{cx:.0f}" y="{cy + 6 + 42:.0f}" text-anchor="middle" '
                f'fill="{PURPLE_TEXT}" font-size="{NODE_FS}" font-weight="600">{t}</text>')
        self._saw(x2, cy + h / 2)

    def axis(self, x1, x2, y, ticks):
        """A time line with labelled ticks — hours, so the width means something."""
        self.parts.append(
            f'<path d="M{x1:.0f} {y} H{x2:.0f}" fill="none" stroke="{ARROW}" stroke-width="1.6"/>')
        for tx, tl in ticks:
            self.parts.append(
                f'<path d="M{tx:.0f} {y - 6} V{y + 7}" stroke="{ARROW}" stroke-width="1.6"/>')
            self.parts.append(
                f'<text x="{tx:.0f}" y="{y + 34}" text-anchor="middle" fill="{LABEL_TEXT}" '
                f'font-size="{LABEL_FS}">{tl}</text>')
        self._saw(x2, y + 34)

    # ---- edges --------------------------------------------------------
    def arrow(self, x1, y1, x2, y2, dashed=False, curve=0):
        d = (f"M{x1:.0f} {y1:.0f} C{x1 + curve:.0f} {y1:.0f}, {x2 - curve:.0f} {y2:.0f}, "
             f"{x2:.0f} {y2:.0f}") if curve else f"M{x1:.0f} {y1:.0f} L{x2:.0f} {y2:.0f}"
        dash = ' stroke-dasharray="7 7"' if dashed else ""
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{ARROW}" stroke-width="{STROKE}"{dash} '
            f'marker-end="url(#{head()})"/>')
        self._saw(max(x1, x2), max(y1, y2))

    def elbow(self, pts, dashed=False):
        d = "M" + " L".join(f"{x:.0f} {y:.0f}" for x, y in pts)
        dash = ' stroke-dasharray="7 7"' if dashed else ""
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{ARROW}" stroke-width="{STROKE}"{dash} '
            f'marker-end="url(#{head()})"/>')
        for x, y in pts:
            self._saw(x, y)

    def ring(self, cx, cy, r, stations):
        """A flywheel: nodes on a circle, arcs between them, clockwise from the top.

        Each arc starts and ends clear of the box it touches, worked out from that
        box's own width, so a wide station never has an arrow running under it.
        """
        import math
        n = len(stations)
        placed = []
        for k, (label, kind, *rest) in enumerate(stations):
            w = rest[0] if rest else box_w(label)
            th = math.radians(-90 + k * (360 / n))
            x, y = cx + r * math.cos(th), cy + r * math.sin(th)
            (self.box if kind in ("green", "purple") else self.term)(
                x, y, label, **({"kind": kind} if kind in ("green", "purple") else {}), w=w)
            placed.append((x, y, w, -90 + k * (360 / n)))
        # Arcs ride the same circle the boxes sit on and run between them, which is
        # how the reference flywheels read as wheels. That only works if the circle
        # is wide relative to the boxes — keep labels to two or three words.
        for k in range(n):
            _, _, w1, a1 = placed[k]
            _, _, w2, a2 = placed[(k + 1) % n]
            g1 = math.degrees(math.atan((w1 / 2 + 30) / r))
            g2 = math.degrees(math.atan((w2 / 2 + 40) / r))
            ra = r
            s1, s2 = math.radians(a1 + g1), math.radians(a2 - g2)
            p1 = (cx + ra * math.cos(s1), cy + ra * math.sin(s1))
            p2 = (cx + ra * math.cos(s2), cy + ra * math.sin(s2))
            self.parts.append(
                f'<path d="M{p1[0]:.0f} {p1[1]:.0f} A{ra:.0f} {ra:.0f} 0 0 1 {p2[0]:.0f} {p2[1]:.0f}" '
                f'fill="none" stroke="{ARROW}" stroke-width="{STROKE}" marker-end="url(#{head()})"/>')
            self._saw(cx + ra, cy + ra)
        return placed

    def wheel(self, cx, cy, r, stations, hub_r=0, hub_lines=(), hub_fill=None, fs=None,
              start=-90, arc=None, hub_line=None, hub_text=None):
        """A flywheel drawn the way the ones people remember are drawn.

        The reference (Amazon's) works because of three things this deliberately
        copies. The stations are *bare words sitting on the circle* — a box around
        each one turns a wheel into six separate objects and the circle stops being
        the subject. The arcs are heavy enough to read as one continuous rim rather
        than six thin connectors. And the middle is not empty: the payoff the loop
        produces is a filled disc, the heaviest thing in the frame, so the eye lands
        in the centre and the ring explains itself around it.

        `stations` are (label, colour) and are placed clockwise from the top. The
        arc between two stations stops clear of each one's own text width, so a long
        label never has the rim running through it.
        """
        import math
        fs = fs or NODE_FS
        n = len(stations)
        placed = []
        for k, st in enumerate(stations):
            label, colour = (st + (GREEN_TEXT,))[:2] if len(st) < 2 else st[:2]
            # `start` rotates the whole ring. Two concentric rings that both begin at
            # the top put a station directly above a station, and the arrow joining
            # them then runs along a radius through both labels.
            deg = start + k * (360 / n)
            th = math.radians(deg)
            x, y = cx + r * math.cos(th), cy + r * math.sin(th)
            self.parts.append(
                f'<text x="{x:.0f}" y="{y + fs / 3:.0f}" text-anchor="middle" '
                f'fill="{colour}" font-size="{fs}" font-weight="700">{label}</text>')
            self._saw(x + text_w(label, fs) / 2, y + H / 2)
            placed.append((x, y, text_w(label, fs), deg))

        for k in range(n):
            _, _, w1, a1 = placed[k]
            _, _, w2, a2 = placed[(k + 1) % n]
            g1 = math.degrees(math.atan((w1 / 2 + 34) / r))
            g2 = math.degrees(math.atan((w2 / 2 + 44) / r))
            s1, s2 = math.radians(a1 + g1), math.radians(a2 - g2)
            p1 = (cx + r * math.cos(s1), cy + r * math.sin(s1))
            p2 = (cx + r * math.cos(s2), cy + r * math.sin(s2))
            # The rim carries the loop's own colour. With two rings on one drawing a
            # grey rim says nothing about which loop an arc belongs to, and the
            # colour on the labels alone is not enough to tell them apart.
            self.parts.append(
                f'<path d="M{p1[0]:.0f} {p1[1]:.0f} A{r:.0f} {r:.0f} 0 0 1 '
                f'{p2[0]:.0f} {p2[1]:.0f}" fill="none" stroke="{arc or ARROW}" '
                f'stroke-width="{RIM}" marker-end="url(#{head(arc, heavy=True)})"/>')
            self._saw(cx + r, cy + r)

        if hub_r:
            # Tinted paper with ink on it, like every other shape in the system —
            # a solid disc is the one thing here that would read as a block of colour.
            self.parts.append(
                f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{hub_r}" '
                f'fill="{hub_fill or PURPLE_FILL}" stroke="{hub_line or PURPLE_LINE}" '
                f'stroke-width="{BORDER}"/>')
            top = cy - (len(hub_lines) - 1) * 21 + 11
            for i, ln in enumerate(hub_lines):
                self.parts.append(
                    f'<text x="{cx:.0f}" y="{top + i * 42:.0f}" text-anchor="middle" '
                    f'fill="{hub_text or PURPLE_TEXT}" font-size="34" font-weight="700" '
                    f'letter-spacing="0.5">{ln}</text>')
        return placed

    def block(self, cx, cy, lines, colour=None, fs=None, weight=700, lead=None):
        """A short stack of centred text with no box around it.

        Every station on a multi-loop flywheel is drawn this way. Boxes would put
        forty borders on the page and the loops would disappear behind them; size
        and colour carry the hierarchy instead — core stations large and dark,
        feeder stations smaller and in their loop's colour.
        """
        fs = fs or CORE_FS
        lead = lead or fs + 9
        top = cy - (len(lines) - 1) * lead / 2
        for i, ln in enumerate(lines):
            self.parts.append(
                f'<text x="{cx:.0f}" y="{top + i * lead + fs / 3:.0f}" text-anchor="middle" '
                f'fill="{colour or LABEL_TEXT}" font-size="{fs}" '
                f'font-weight="{weight}">{ln}</text>')
            self._saw(cx + text_w(ln, fs) / 2, top + i * lead + fs)
        return max(text_w(ln, fs) for ln in lines)

    def bow(self, x1, y1, x2, y2, lift=60, width=None, colour=None, dashed=False):
        """One curved arrow between two free-floating points.

        `lift` bends it perpendicular to the straight line between them — positive
        one way, negative the other — which is the whole vocabulary needed to keep
        thirty arrows from crossing each other on a dense diagram.
        """
        import math
        dx, dy = x2 - x1, y2 - y1
        L = math.hypot(dx, dy) or 1
        px, py = -dy / L, dx / L
        qx, qy = (x1 + x2) / 2 + px * lift, (y1 + y2) / 2 + py * lift
        dash = ' stroke-dasharray="8 8"' if dashed else ""
        self.parts.append(
            f'<path d="M{x1:.0f} {y1:.0f} Q{qx:.0f} {qy:.0f} {x2:.0f} {y2:.0f}" fill="none" '
            f'stroke="{colour or ARROW}" stroke-width="{width or STROKE}"{dash} '
            f'marker-end="url(#{head(colour, heavy=(width or STROKE) >= RIM)})"/>')
        self._saw(max(x1, x2), max(y1, y2))

    def dot(self, x, y, r=9, colour=None):
        """Where a second loop leaves the wheel. The reference marks this point, and
        marking it is what makes a fork read as a fork instead of a collision."""
        self.parts.append(
            f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{r}" fill="{colour or LABEL_TEXT}"/>')

    def key(self, x, y, items, title=None):
        """The legend. Each entry names one advantage and the colour that carries it."""
        if title:
            self.parts.append(
                f'<text x="{x:.0f}" y="{y:.0f}" fill="{LABEL_TEXT}" font-size="{SAT_FS}" '
                f'font-weight="700">{title}</text>')
            y += 40
        for i, (colour, label) in enumerate(items):
            ry = y + i * 42
            self.parts.append(
                f'<rect x="{x:.0f}" y="{ry - 17:.0f}" width="24" height="24" rx="5" '
                f'fill="{colour}"/>')
            self.parts.append(
                f'<text x="{x + 40:.0f}" y="{ry + 3:.0f}" fill="{LABEL_TEXT}" '
                f'font-size="{SAT_FS}">{label}</text>')
            self._saw(x + 40 + text_w(label, SAT_FS), ry + 20)

    def inner_arc(self, cx, cy, r, a1, a2, label=()):
        """A shortcut that stays inside the rim instead of cutting across it.

        A straight chord through the middle reads as the wheel being broken in half;
        an arc in the band between hub and rim reads as what it is — a faster way
        round that skips most of the loop.
        """
        import math
        p1 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
        p2 = (cx + r * math.cos(math.radians(a2)), cy + r * math.sin(math.radians(a2)))
        self.parts.append(
            f'<path d="M{p1[0]:.0f} {p1[1]:.0f} A{r:.0f} {r:.0f} 0 0 0 '
            f'{p2[0]:.0f} {p2[1]:.0f}" fill="none" stroke="{ARROW}" stroke-width="{STROKE}" '
            f'stroke-dasharray="8 8" marker-end="url(#{head()})"/>')
        if label:
            mid = math.radians((a1 + a2) / 2)
            lx, ly = cx + (r - 34) * math.cos(mid), cy + (r - 34) * math.sin(mid)
            self.label(lx, ly, *label)

    def hub(self, cx, cy, *lines):
        for i, ln in enumerate(lines):
            self.parts.append(
                f'<text x="{cx:.0f}" y="{cy + i * 34:.0f}" text-anchor="middle" '
                f'fill="{LABEL_TEXT}" font-size="24">{ln}</text>')

    def label(self, cx, cy, *lines, anchor="middle"):
        for i, ln in enumerate(lines):
            self.parts.append(
                f'<text x="{cx:.0f}" y="{cy + i * 28:.0f}" text-anchor="{anchor}" '
                f'fill="{LABEL_TEXT}" font-size="{LABEL_FS}">{ln}</text>')
            # measure where the text actually ends, which depends on the anchor —
            # otherwise an end-anchored label inflates the page by half its width
            w = text_w(ln, LABEL_FS)
            self._saw(cx + (0 if anchor == "end" else w if anchor == "start" else w / 2),
                      cy + i * 28)

    # ---- output -------------------------------------------------------
    def svg(self):
        defs = []
        for colour, sfx in MARKERS.items():
            defs.append(
                f'    <marker id="h{sfx}" markerWidth="14" markerHeight="14" refX="11" refY="5" '
                f'orient="auto" markerUnits="userSpaceOnUse">\n'
                f'      <path d="M1.5 1 L10 5 L1.5 9" fill="none" stroke="{colour}" '
                f'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>\n'
                f'    </marker>')
            defs.append(
                f'    <marker id="H{sfx}" markerWidth="19" markerHeight="19" refX="13" refY="6.5" '
                f'orient="auto" markerUnits="userSpaceOnUse">\n'
                f'      <path d="M2 1.5 L12.5 6.5 L2 11.5" fill="none" stroke="{colour}" '
                f'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>\n'
                f'    </marker>')
        top = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {self.w} {self.h}" '
            f'font-family="{FONT}">\n'
            f'  <defs>\n' + "\n".join(defs) + '\n  </defs>\n'
            f'  <rect width="{self.w}" height="{self.h}" fill="#fff"/>\n  ')
        return top + "\n  ".join(self.parts) + "\n</svg>\n"

    def write(self, path):
        # the drawing decides the page, not the other way round
        self.w = max(self.w, int(self.maxx) + 100)
        self.h = max(self.h, int(self.maxy) + 60)
        pathlib.Path(path).write_text(self.svg())
        return path
