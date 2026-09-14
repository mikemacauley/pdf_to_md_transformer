"""Read page candidates before the legacy pipeline normalizes or drops them."""

from pdfminer.converter import PDFPageAggregator
from pdfminer.pdfinterp import PDFPageInterpreter


class VisibilityProbe(PDFPageAggregator):
    def __init__(self, manager):
        super().__init__(manager)
        self.conflict = False
        self.glyph_boxes = []

    def render_string(self, textstate, seq, ncs, graphicstate):
        if textstate.render in (3, 7) and seq:
            self.conflict = True
        return super().render_string(textstate, seq, ncs, graphicstate)

    def render_char(self, *args, **kwargs):
        advance = super().render_char(*args, **kwargs)
        if self.cur_item._objs:
            self.glyph_boxes.append(self.cur_item._objs[-1].bbox)
        return advance

    def paint_path(self, gstate, stroke, fill, evenodd, path):
        start = len(self.cur_item._objs)
        super().paint_path(gstate, stroke, fill, evenodd, path)
        if fill:
            for obj in self.cur_item._objs[start:]:
                a = obj.bbox
                if any(
                    min(a[2], b[2]) > max(a[0], b[0])
                    and min(a[3], b[3]) > max(a[1], b[1])
                    for b in self.glyph_boxes
                ):
                    self.conflict = True


def visibility_conflict(page):
    """Detect nonrendering text and overlapping filled panels conservatively.

    This is a limited detector, not proof that a PDF is safe to redact. Filled
    paths painted after overlapping glyphs are withheld. Background panels
    painted before text do not conflict. This does not qualify all PDF compositing.
    """
    probe = VisibilityProbe(page.pdf.rsrcmgr)
    try:
        PDFPageInterpreter(page.pdf.rsrcmgr, probe).process_page(page.page_obj)
        return probe.conflict
    finally:
        probe.close()


def raster_union_area(images, bbox):
    """Exact rectangle union, so overlapping images cannot inflate routing."""
    left, top, right, bottom = bbox
    rects = []
    for image in images:
        x0, y0, x1, y1 = (
            max(left, image["x0"]),
            max(top, image["top"]),
            min(right, image["x1"]),
            min(bottom, image["bottom"]),
        )
        if x1 > x0 and y1 > y0:
            rects.append((x0, y0, x1, y1))
    xs = sorted({x for r in rects for x in (r[0], r[2])})
    area = 0
    for a, b in zip(xs, xs[1:]):
        ys = sorted((r[1], r[3]) for r in rects if r[0] < b and r[2] > a)
        end = -float("inf")
        height = 0
        for lo, hi in ys:
            height += max(0, hi - max(lo, end))
            end = max(end, hi)
        area += (b - a) * height
    return area
