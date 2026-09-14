"""Sidebar / callout detection.

A callout box is set apart from the body visually, and publishers do that
in three ways.  All three are recognised here:

  * **filled** -- a shaded background rectangle (the original case);
  * **ruled** -- a box drawn with a border and no fill, either as a
    stroked rectangle or as four separate lines;
  * **typographic** -- no box at all: a run of lines inset from both
    sides and set in a different size or face (pull quotes, abstracts,
    notes).

The first two are geometry and are found before the text is laid out.
The third can only be judged once lines exist, and is by far the easiest
to get wrong -- an ordinary indented continuation must not become a
blockquote -- so it is gated hard: the block must be inset on *both*
sides, must not sit on any column margin (which is what a second column
looks like), must be separated above and below, and must differ from the
body in size or face.
"""

from __future__ import annotations

from collections import Counter
from typing import List, Optional, Tuple

MIN_W = 90.0            # a box narrower than this is a rule or a bullet
MIN_H = 30.0
MIN_AREA_FRAC = 0.01    # of the page
MAX_AREA_FRAC = 0.85
CORNER_TOL = 3.0        # pt; how far box corners may miss each other

# typographic blocks
MIN_BLOCK_LINES = 2
MAX_BLOCK_LINES = 14
MIN_INSET = 10.0        # pt the block must be pulled in from the text margin
MARGIN_TOL = 6.0        # pt; how close to a column margin still counts as on it
GAP_FACTOR = 1.4        # separation above/below, in line heights
SIZE_TOL = 0.4          # pt; smaller size differences aren't a signal
MIN_MARGIN_LINES = 3    # lines sharing an x before it counts as a column margin


def _center_in(bbox, x, y) -> bool:
    x0, top, x1, bottom = bbox
    return x0 <= x <= x1 and top <= y <= bottom


def _plausible(bbox, page_area, table_bboxes, kept) -> bool:
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    if w < MIN_W or h < MIN_H:
        return False
    area = w * h
    if area > MAX_AREA_FRAC * page_area or area < MIN_AREA_FRAC * page_area:
        return False
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    if any(_center_in(tb, cx, cy) for tb in table_bboxes):
        return False
    return not any(_center_in(existing, cx, cy) for existing in kept)


def _line_boxes(page) -> List[Tuple[float, float, float, float]]:
    """Rectangles drawn as four separate lines (a common border style)."""
    horiz, vert = [], []
    for ln in page.lines:
        if abs(ln["bottom"] - ln["top"]) <= 1.5 and ln["x1"] - ln["x0"] > MIN_W:
            horiz.append(ln)
        elif abs(ln["x1"] - ln["x0"]) <= 1.5 and ln["bottom"] - ln["top"] > MIN_H:
            vert.append(ln)

    horiz.sort(key=lambda ln: (ln["top"], ln["x0"]))
    boxes = []
    for i, top_ln in enumerate(horiz):
        for bot_ln in horiz[i + 1:]:
            if bot_ln["top"] - top_ln["top"] < MIN_H:
                continue
            x0 = max(top_ln["x0"], bot_ln["x0"])
            x1 = min(top_ln["x1"], bot_ln["x1"])
            if x1 - x0 < MIN_W:
                continue
            left = [v for v in vert if abs(v["x0"] - x0) <= CORNER_TOL]
            right = [v for v in vert if abs(v["x0"] - x1) <= CORNER_TOL]
            if not left or not right:
                continue
            box = (x0, top_ln["top"], x1, bot_ln["top"])
            if all(any(v["top"] <= box[1] + CORNER_TOL
                       and v["bottom"] >= box[3] - CORNER_TOL
                       for v in side) for side in (left, right)):
                boxes.append(box)
    return boxes


def detect_boxed_regions(page, table_bboxes=()) -> List[tuple]:
    """Filled and ruled callout boxes on the page, largest first."""
    page_area = float(page.width) * float(page.height)

    filled, ruled = [], []
    for r in page.rects:
        bbox = (r["x0"], r["top"], r["x1"], r["bottom"])
        if r.get("fill"):
            filled.append(bbox)
        elif r.get("stroke"):
            ruled.append(bbox)
    ruled.extend(_line_boxes(page))

    # Filled boxes first: a shaded panel is the least ambiguous signal, and
    # taking it first means a border drawn around it can't claim the region.
    regions: List[tuple] = []
    for group in (filled, ruled):
        group.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
        for bbox in group:
            if _plausible(bbox, page_area, table_bboxes, regions):
                regions.append(bbox)
    return regions


# --------------------------------------------------------------------------
# typographic (unboxed) callouts
# --------------------------------------------------------------------------

def _column_margins(lines, edge: str) -> List[float]:
    """x positions shared by enough lines to be a column edge, not an inset."""
    counts = Counter(round(getattr(ln, edge)) for ln in lines)
    return [x for x, n in counts.items() if n >= MIN_MARGIN_LINES]


def _on_margin(x: float, margins: List[float]) -> bool:
    return any(abs(x - m) <= MARGIN_TOL for m in margins)


def detect_typographic_blocks(lines, body_size: float) -> List[Tuple[int, int]]:
    """Index ranges [start, end) of unboxed callout blocks in ``lines``.

    ``lines`` must be in reading order.  Returns nothing at all unless a
    block clears every gate -- the cost of a false positive (a paragraph
    turned into a blockquote) is higher than the cost of a miss (a
    callout that reads as an ordinary paragraph, i.e. today's behaviour).
    """
    if len(lines) < MIN_BLOCK_LINES + 2:
        return []

    left_margins = _column_margins(lines, "x0")
    if not left_margins:
        return []
    # Right edges are ragged in unjustified text, so they cluster badly;
    # the text block's outer edge is the reliable reference instead.
    right_edge = max(ln.x1 for ln in lines)

    dominant_font = Counter(
        getattr(ln, "font", "") for ln in lines if getattr(ln, "font", "")
    ).most_common(1)
    dominant_font = dominant_font[0][0] if dominant_font else ""

    def inset(ln) -> bool:
        # Not on any column's left margin (that is what a second column
        # looks like), pulled in from the leftmost margin, and short of the
        # text block's right edge.
        return (not _on_margin(ln.x0, left_margins)
                and ln.x0 >= min(left_margins) + MIN_INSET
                and ln.x1 <= right_edge - MIN_INSET)

    blocks: List[Tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if not inset(lines[i]):
            i += 1
            continue
        j = i
        while j < len(lines) and inset(lines[j]):
            j += 1
        run = lines[i:j]
        if (MIN_BLOCK_LINES <= len(run) <= MAX_BLOCK_LINES
                and i > 0 and j < len(lines)
                and _separated(lines, i, j)
                and _typographically_distinct(run, body_size, dominant_font)):
            blocks.append((i, j))
        i = max(j, i + 1)
    return blocks


def _separated(lines, i: int, j: int) -> bool:
    """Blank space above and below, so it isn't a continued paragraph."""
    run = lines[i:j]
    height = max((ln.bottom - ln.top) for ln in run)
    above = run[0].top - lines[i - 1].bottom
    below = lines[j].top - run[-1].bottom
    return above >= GAP_FACTOR * height and below >= GAP_FACTOR * height


def _typographically_distinct(run, body_size: float, dominant_font: str) -> bool:
    sizes = [ln.size for ln in run]
    median = sorted(sizes)[len(sizes) // 2]
    if abs(median - body_size) >= SIZE_TOL:
        return True
    fonts = {getattr(ln, "font", "") for ln in run}
    return bool(dominant_font) and fonts and dominant_font not in fonts


def block_bbox(run) -> Tuple[float, float, float, float]:
    return (min(ln.x0 for ln in run), min(ln.top for ln in run),
            max(ln.x1 for ln in run), max(ln.bottom for ln in run))


def dominant_body_size(lines) -> Optional[float]:
    """Character-weighted modal line size -- the page's body text size."""
    counter: Counter = Counter()
    for ln in lines:
        counter[round(ln.size * 2) / 2] += ln.nchars
    return counter.most_common(1)[0][0] if counter else None
