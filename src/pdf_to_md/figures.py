"""Figure detection, caption binding and (optional) image extraction.

A figure is either an embedded raster image or a cluster of vector
drawing primitives (curves/lines) that isn't part of a table or sidebar.
Detection is purely geometric and therefore deterministic.

Figures are never guessed at aggressively: a candidate region that turns
out to contain a lot of text is discarded and its words flow back into
the body, the same fail-safe the borderless-table detector uses.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from .fileio import atomic_output
from .ocr import check_render_size

# "Figure 3.", "Fig. 3:", "FIGURE 12 --", "Chart 2", "Scheme 1", "Plate IV"
CAPTION_RE = re.compile(
    r"^(figure|fig\.|fig|chart|scheme|plate|exhibit)\s*"
    r"([0-9]{1,3}[a-z]?|[ivxlc]{1,6})\b",
    re.IGNORECASE,
)

MIN_W = 48.0            # pt; smaller marks are rules, bullets, logos
MIN_H = 32.0
MIN_VECTOR_PARTS = 6    # primitives needed before a cluster counts
CLUSTER_GAP = 12.0      # pt; primitives at least this close merge
MAX_WORDS_INSIDE = 40   # more text than this => not a figure region
CAPTION_GAP = 42.0      # pt; how far from the figure a caption may sit
DPI = 300               # extraction resolution


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _overlap(a, b) -> float:
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, ix) * max(0.0, iy)


def _merge(a, b):
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def _near(a, b, gap: float = CLUSTER_GAP) -> bool:
    return (a[0] - gap <= b[2] and b[0] - gap <= a[2]
            and a[1] - gap <= b[3] and b[1] - gap <= a[3])


def _cluster(boxes: List[tuple]) -> List[tuple]:
    """Union boxes that touch or nearly touch, until nothing merges."""
    out = list(boxes)
    changed = True
    while changed:
        changed = False
        merged: List[tuple] = []
        for box in out:
            for i, kept in enumerate(merged):
                if _near(box, kept):
                    merged[i] = _merge(kept, box)
                    changed = True
                    break
            else:
                merged.append(box)
        out = merged
    return out


def detect_figure_regions(page, exclude_bboxes=()) -> List[Tuple[float, ...]]:
    """Bounding boxes of figures on the page, in reading (top-down) order.

    ``exclude_bboxes`` are regions already claimed by tables or sidebars.
    """
    page_area = float(page.width) * float(page.height)
    candidates: List[tuple] = []

    for im in page.images:
        candidates.append((im["x0"], im["top"], im["x1"], im["bottom"]))

    parts = []
    for obj in list(page.curves) + list(page.lines):
        b = (obj["x0"], obj["top"], obj["x1"], obj["bottom"])
        if _area(b) <= 0 and (b[2] - b[0]) < 1 and (b[3] - b[1]) < 1:
            continue
        parts.append(b)
    for cluster in _cluster(parts):
        members = sum(1 for p in parts if _overlap(p, cluster) > 0
                      or _near(p, cluster, 0.5))
        if members >= MIN_VECTOR_PARTS:
            candidates.append(cluster)

    regions: List[tuple] = []
    for b in candidates:
        w, h = b[2] - b[0], b[3] - b[1]
        if w < MIN_W or h < MIN_H:
            continue
        a = _area(b)
        if a > 0.92 * page_area:      # full-page background, not a figure
            continue
        if any(_overlap(b, ex) > 0.5 * a for ex in exclude_bboxes):
            continue
        if any(_overlap(b, k) > 0.5 * min(a, _area(k)) for k in regions):
            continue                  # duplicate / nested candidate
        regions.append(b)

    regions.sort(key=lambda b: (round(b[1], 1), b[0]))
    return regions


def looks_like_caption(text: str) -> bool:
    return bool(CAPTION_RE.match(text.strip()))


def caption_label(text: str) -> Optional[str]:
    """"Figure 3: Yield by season." -> "Figure 3" (None if not a caption)."""
    m = CAPTION_RE.match(text.strip())
    if not m:
        return None
    word = m.group(1).rstrip(".").capitalize()
    if word.lower() == "fig":
        word = "Figure"
    return f"{word} {m.group(2)}"


def bind_caption(region, lines) -> Optional[int]:
    """Index of the caption line for ``region``, or None.

    Prefers the nearest caption line below the figure (the overwhelmingly
    common placement), then the nearest above.  The line must overlap the
    figure horizontally and sit within CAPTION_GAP points of it.
    """
    x0, top, x1, bottom = region
    below, above = [], []
    for i, ln in enumerate(lines):
        if not looks_like_caption(ln.text):
            continue
        if min(ln.x1, x1) - max(ln.x0, x0) <= 0:
            continue  # different column
        if 0 <= ln.top - bottom <= CAPTION_GAP:
            below.append((ln.top - bottom, i))
        elif 0 <= top - ln.bottom <= CAPTION_GAP:
            above.append((top - ln.bottom, i))
    for bucket in (below, above):
        if bucket:
            return min(bucket)[1]
    return None


def extract_image(page, region, out_path: str) -> None:
    """Render ``region`` of the page to a PNG at DPI (deterministic)."""
    x0, top, x1, bottom = region
    left, page_top, right, page_bottom = page.bbox
    box = (max(left, x0), max(page_top, top),
           min(right, x1), min(page_bottom, bottom))
    # pdfplumber renders the parent page before cropping the raster.
    check_render_size(float(page.width), float(page.height), DPI)
    img = page.crop(box).to_image(resolution=DPI).original
    with atomic_output(out_path) as output:
        img.convert("RGB").save(output, format="PNG", optimize=True)
