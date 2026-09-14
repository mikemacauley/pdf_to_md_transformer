"""Shared low-level geometry: word boxes -> lines, and reading order.

All functions operate on plain word dicts with keys
``x0, x1, top, bottom, text`` (and optionally ``size``), regardless of
whether the words came from the PDF text layer or from OCR.  Everything
is deterministic.
"""

from __future__ import annotations

import re
import math
import statistics
from dataclasses import dataclass
from typing import List, Optional

from .equations import math_evidence

_CTRL_RE = re.compile(r"[\x01-\x08\x0b\x0c\x0e-\x1f\x7f]")


def scan_positions(start, stop, step):
    """Bound coordinate scans even for corrupt or extreme PDF geometry."""
    if (not math.isfinite(start) or not math.isfinite(stop)
            or stop - start > 150_000 or start + step == start):
        raise ValueError("invalid or excessive PDF coordinate range")
    x = start
    while x <= stop:
        yield x
        x += step


def clean_text(s: str) -> str:
    s = s.replace("\x00", "")
    s = _CTRL_RE.sub("", s)
    return s.strip()


@dataclass
class Line:
    """One visual line of text."""
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float          # median glyph size on the line
    nchars: int
    math: float = 0.0    # fraction of glyphs that look mathematical
    font: str = ""       # most common font on the line (vector path only)

    kind: str = "line"   # constant; used by the ordering code


def words_to_line_groups(words, split_columns: bool = False) -> List[List[dict]]:
    """Cluster positioned words into visual lines; returns word groups.

    With ``split_columns=True``, a baseline group is further split at
    large horizontal gaps so that side-by-side columns sharing a
    baseline become separate lines.  Table reconstruction must keep
    ``split_columns=False`` -- a table row IS one group with large gaps.
    """
    if not words:
        return []
    words = sorted(words, key=lambda w: (round(w["top"], 1), w["x0"]))
    groups: List[List[dict]] = []
    current: List[dict] = []
    for w in words:
        if not current:
            current = [w]
            continue
        ref = current[0]
        # same line if vertical overlap of at least half the smaller height
        h = min(ref["bottom"] - ref["top"], w["bottom"] - w["top"])
        overlap = min(ref["bottom"], w["bottom"]) - max(ref["top"], w["top"])
        if h > 0 and overlap >= 0.5 * h:
            current.append(w)
        else:
            groups.append(sorted(current, key=lambda x: x["x0"]))
            current = [w]
    if current:
        groups.append(sorted(current, key=lambda x: x["x0"]))

    if not split_columns:
        return groups

    out: List[List[dict]] = []
    for g in groups:
        sizes = sorted(float(x.get("size") or (x["bottom"] - x["top"])) for x in g)
        med = sizes[len(sizes) // 2] if sizes else 10.0
        gap_limit = max(12.0, 1.5 * med)
        piece = [g[0]]
        for w in g[1:]:
            if w["x0"] - piece[-1]["x1"] > gap_limit:
                out.append(piece)
                piece = [w]
            else:
                piece.append(w)
        out.append(piece)
    return out


def _dominant_font(ws) -> str:
    fonts = [w.get("fontname") for w in ws if w.get("fontname")]
    if not fonts:
        return ""
    return statistics.mode(fonts) if len(set(fonts)) > 1 else fonts[0]


def make_line(ws) -> Line:
    ws = sorted(ws, key=lambda w: w["x0"])
    text = clean_text(" ".join(w["text"] for w in ws))
    sizes = [float(w.get("size") or (w["bottom"] - w["top"])) for w in ws]
    return Line(
        text=text,
        x0=min(w["x0"] for w in ws),
        x1=max(w["x1"] for w in ws),
        top=min(w["top"] for w in ws),
        bottom=max(w["bottom"] for w in ws),
        size=statistics.median(sizes),
        nchars=sum(len(w["text"]) for w in ws),
        math=math_evidence(ws),
        font=_dominant_font(ws),
    )


def words_to_lines(words, split_columns: bool = True) -> List[Line]:
    lines = [make_line(g)
             for g in words_to_line_groups(words, split_columns=split_columns)]
    return [ln for ln in lines if ln.text]


def order_items(items: list, content_x0: float, content_x1: float) -> list:
    """Order mixed items (lines / tables / sidebars) into reading order.

    Detects a two-column body via an empty vertical gutter; full-width
    items act as segment separators.  Falls back to plain top-to-bottom
    ordering for single-column pages.
    """
    if not items:
        return []
    width = max(content_x1 - content_x0, 1.0)
    mid = (content_x0 + content_x1) / 2.0

    def is_full(it) -> bool:
        # genuinely wide, or a centered element (title, byline, caption)
        # that substantially straddles the would-be column gutter
        return (it.x1 - it.x0) > 0.66 * width \
            or (it.x0 < mid - 30 and it.x1 > mid + 30)

    fulls = [it for it in items if is_full(it)]
    narrow = [it for it in items if not is_full(it)]

    gutter = find_gutter(narrow, content_x0, width)
    if gutter is None:
        return sorted(items, key=lambda it: (round(it.top, 1), it.x0))

    fulls.sort(key=lambda it: it.top)

    def seg_index(it) -> int:
        i = 0
        for f in fulls:
            if f.bottom - 2 <= it.top:
                i += 1
        return i

    ordered: list = []
    for i in range(len(fulls) + 1):
        if i > 0:
            ordered.append(fulls[i - 1])
        seg = [it for it in narrow if seg_index(it) == i]
        left = sorted((it for it in seg if it.x1 <= gutter + 2), key=lambda it: it.top)
        right = sorted((it for it in seg if it.x0 >= gutter - 2), key=lambda it: it.top)
        ordered.extend(left)
        ordered.extend(right)
    return ordered


def find_gutter(narrow: list, content_x0: float, width: float) -> Optional[float]:
    """Return the x of an empty vertical gutter if the page is two-column."""
    if len(narrow) < 6:
        return None
    lo = content_x0 + 0.30 * width
    hi = content_x0 + 0.70 * width
    candidates = []
    for x in scan_positions(lo, hi, 3.0):
        if all(not (it.x0 - 2 < x < it.x1 + 2) for it in narrow):
            candidates.append(x)
    if not candidates:
        return None
    gutter = statistics.median(candidates)
    left = [it for it in narrow if it.x1 <= gutter + 2]
    right = [it for it in narrow if it.x0 >= gutter - 2]
    if len(left) >= 3 and len(right) >= 3 and len(left) + len(right) == len(narrow):
        return gutter
    return None
