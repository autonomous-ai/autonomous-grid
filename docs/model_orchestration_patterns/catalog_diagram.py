"""Catalog-specific rendering for compact pattern diagrams.

The shared :class:`build_diagrams.Diagram` deliberately keeps its drawing
grammar small.  Catalog figures need one additional readability guarantee:
an arrow must never be visible through its caption.  This subclass preserves
the shared layout and verification logic, but defers edge captions until all
arrows have been drawn and places each caption on an opaque, padded field.
"""

from __future__ import annotations

from build_diagrams import Diagram as BaseDiagram
from build_diagrams import INK, esc


class CatalogDiagram(BaseDiagram):
    """A shared diagram whose edge captions always sit above clear space."""

    README_WIDTH = 960
    MIN_SCREEN_TEXT = 11
    LABEL_FIELD_PAD = 2

    def verify(self):
        problems = super().verify()
        width, height = self._geom()
        scale = min(1.0, self.README_WIDTH / width)
        fields = []
        for edge, box in zip(self.edges, self._label_boxes(width, height)):
            if box is None:
                continue
            screen_size = box[5] * scale
            if screen_size < self.MIN_SCREEN_TEXT:
                problems.append(
                    f"label '{edge['label']}' renders at only "
                    f"{screen_size:.1f}px at {self.README_WIDTH}px"
                )
            field = self._label_field(box)
            fields.append((edge["label"], field))
            x, y, w, h = field
            if x < 0 or y < 0 or x + w > width or y + h > height:
                problems.append(f"label field '{edge['label']}' clipped by viewBox")
            for nid, node in self.nodes.items():
                if node["kind"] == "dot":
                    continue
                if (x < self.xr(nid) and self.xl(nid) < x + w
                        and y < self.bottom(nid) and self.top(nid) < y + h):
                    problems.append(f"label field '{edge['label']}' overlaps node {nid}")
        for i, (label_a, a) in enumerate(fields):
            ax, ay, aw, ah = a
            for label_b, b in fields[i + 1:]:
                bx, by, bw, bh = b
                if (ax < bx + bw and bx < ax + aw
                        and ay < by + bh and by < ay + ah):
                    problems.append(f"label fields '{label_a}' & '{label_b}' overlap")
        return problems

    def render(self):
        # BaseDiagram.render() calls _elabel() for every caption, then _edge()
        # for every arrow.  Record the captions so the overridden _edge() can
        # append them after the final arrow and before the nodes.
        self._pending_catalog_labels = []
        self._catalog_edges_drawn = 0
        return super().render()

    def _elabel(self, p, e, box):
        self._pending_catalog_labels.append((e, box))

    def _edge(self, p, e):
        super()._edge(p, e)
        self._catalog_edges_drawn += 1
        if self._catalog_edges_drawn == len(self.edges):
            for label_edge, label_box in self._pending_catalog_labels:
                self._boxed_edge_label(p, label_edge, label_box)

    @classmethod
    def _label_field(cls, box):
        """Return the padded opaque field behind a resolved edge caption."""
        mx, glyph_cy, bw, bh, anchor = box[:5]
        if anchor == "end":
            x = mx - bw
        elif anchor == "start":
            x = mx
        else:
            x = mx - bw / 2
        y = glyph_cy - bh / 2
        pad = cls.LABEL_FIELD_PAD
        return x - pad, y - pad, bw + 2 * pad, bh + 2 * pad

    @classmethod
    def _boxed_edge_label(cls, p, e, box):
        """Draw an opaque caption field, then its text, above every arrow."""
        lab = e["label"]
        mx, glyph_cy, _, _, anchor, fs = box[:6]
        baseline = glyph_cy + fs * 0.30
        x, y, width, height = cls._label_field(box)

        p.append(
            f'  <rect class="edge-label-bg" x="{x:.0f}" '
            f'y="{y:.0f}" width="{width:.0f}" '
            f'height="{height:.0f}" rx="5" fill="#fff"/>'
        )
        p.append(
            f'  <text class="edge-label" x="{mx:.0f}" y="{baseline:.0f}" '
            f'text-anchor="{anchor}" fill="{INK}" font-size="{fs}">'
            f'{esc(lab)}</text>'
        )


# Keep builder call sites concise while making the catalog-only rendering
# behavior explicit at the import boundary.
Diagram = CatalogDiagram
