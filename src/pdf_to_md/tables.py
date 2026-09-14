"""Borderless ("booktabs") table detection and reconstruction.

Scientific tables are usually set with horizontal rules only (top rule,
mid rule, bottom rule) and no vertical lines, which lattice detection
cannot see.  This module finds regions bounded by stacked horizontal
rules and reconstructs the table grid from word-box alignment:

  1. Find horizontal rules (thin vector lines/rects, or -- for scans --
     pixel-run lines supplied by the OCR module).
  2. Group stacked rules with similar x-extent into candidate regions.
  3. Inside a region, find column boundaries as vertical strips that
     almost no line of words crosses ("gap clustering").
  4. Assign words to cells; merge wrapped continuation rows.
  5. Accept only if the result actually looks like a table
     (>= 2 columns, >= 2 rows, mostly non-empty cells) -- otherwise the
     words flow back into ordinary body text, so a false candidate can
     never destroy content.

Deterministic throughout.
"""

from __future__ import annotations

from typing import List, Optional

from .geometry import clean_text, scan_positions, words_to_line_groups

MIN_RULE_WIDTH = 40.0        # pt: shorter strokes are not table rules
MAX_RULE_THICKNESS = 2.5     # pt
MAX_REGION_HEIGHT = 340.0    # pt: sanity cap for a rule-bounded table
MAX_RULE_GAP = 300.0         # pt: max distance between stacked rules
CUT_STEP = 1.5               # pt: x scan step for column gaps
MIN_GAP_WIDTH = 3.0          # pt: minimum word-free strip for a column cut


def find_h_rules_vector(page) -> List[dict]:
    """Horizontal rules from the vector layer (lines + thin filled rects)."""
    rules = []
    for ln in page.lines:
        if abs(ln["bottom"] - ln["top"]) <= MAX_RULE_THICKNESS \
                and (ln["x1"] - ln["x0"]) >= MIN_RULE_WIDTH:
            rules.append({"x0": ln["x0"], "x1": ln["x1"],
                          "top": (ln["top"] + ln["bottom"]) / 2.0})
    for r in page.rects:
        if (r["bottom"] - r["top"]) <= MAX_RULE_THICKNESS \
                and (r["x1"] - r["x0"]) >= MIN_RULE_WIDTH:
            rules.append({"x0": r["x0"], "x1": r["x1"],
                          "top": (r["top"] + r["bottom"]) / 2.0})
    rules.sort(key=lambda r: (r["top"], r["x0"]))
    return rules


def _x_overlap_frac(a, b) -> float:
    ov = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
    denom = min(a["x1"] - a["x0"], b["x1"] - b["x0"])
    return ov / denom if denom > 0 else 0.0


def group_rule_regions(rules: List[dict], skip_bboxes=()) -> List[tuple]:
    """Cluster stacked, x-aligned rules into candidate table regions."""
    clusters: List[List[dict]] = []
    for rule in sorted(rules, key=lambda r: r["top"]):
        placed = False
        for cl in clusters:
            last = cl[-1]
            if rule["top"] - last["top"] <= MAX_RULE_GAP \
                    and _x_overlap_frac(rule, last) >= 0.7:
                cl.append(rule)
                placed = True
                break
        if not placed:
            clusters.append([rule])

    regions = []
    for cl in clusters:
        if len(cl) < 2:
            continue
        x0 = min(r["x0"] for r in cl)
        x1 = max(r["x1"] for r in cl)
        top = min(r["top"] for r in cl)
        bottom = max(r["top"] for r in cl)
        if bottom - top < 10 or bottom - top > MAX_REGION_HEIGHT:
            continue
        cx, cy = (x0 + x1) / 2, (top + bottom) / 2
        if any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in skip_bboxes):
            continue  # already a lattice table
        regions.append((x0, top, x1, bottom))
    return regions


def _column_cuts(groups: List[List[dict]]) -> List[float]:
    """x positions of word-free vertical strips crossing (almost) no line."""
    if not groups:
        return []
    x_lo = min(w["x0"] for g in groups for w in g)
    x_hi = max(w["x1"] for g in groups for w in g)
    n = len(groups)
    tolerate = max(1, int(0.2 * n))  # spanning header cells may cross a cut

    free_xs = []
    for x in scan_positions(x_lo, x_hi, CUT_STEP):
        crossing = sum(
            1 for g in groups
            if any(w["x0"] - 1.0 < x < w["x1"] + 1.0 for w in g)
        )
        if crossing <= tolerate:
            free_xs.append(x)

    # group consecutive free positions into gaps; keep interior gaps
    cuts = []
    start = prev = None
    for p in free_xs + [None]:
        if p is not None and (prev is None or p - prev <= CUT_STEP + 0.1):
            if start is None:
                start = p
            prev = p
            continue
        if start is not None and prev is not None:
            if prev - start >= MIN_GAP_WIDTH and start > x_lo + 2 and prev < x_hi - 2:
                cuts.append((start + prev) / 2.0)
        start = prev = p if p is not None else None
    return cuts


def _assign_cells(groups: List[List[dict]], cuts: List[float]) -> List[List[str]]:
    bounds = cuts + [float("inf")]
    rows = []
    for g in groups:
        cells = [[] for _ in range(len(bounds))]
        for w in g:
            cx = (w["x0"] + w["x1"]) / 2.0
            for ci, b in enumerate(bounds):
                if cx < b:
                    cells[ci].append(w)
                    break
        rows.append([clean_text(" ".join(w["text"] for w in c)) for c in cells])
    return rows


def _merge_continuation_rows(rows: List[List[str]]) -> List[List[str]]:
    """A row that is mostly empty continues the row above (wrapped cell)."""
    merged: List[List[str]] = []
    for row in rows:
        nonempty = sum(1 for c in row if c)
        if merged and row and not row[0] and nonempty <= max(1, len(row) // 3):
            for ci, c in enumerate(row):
                if c:
                    merged[-1][ci] = (merged[-1][ci] + " " + c).strip()
        else:
            merged.append(list(row))
    return merged


def reconstruct_table(words: List[dict]) -> Optional[List[List[str]]]:
    """Rebuild a table from word boxes by column alignment; None if it
    does not convincingly look like a table."""
    groups = words_to_line_groups(words)
    groups = [g for g in groups if g]
    if len(groups) < 2:
        return None

    cuts = _column_cuts(groups)
    if not cuts:
        return None

    rows = _assign_cells(groups, cuts)
    rows = _merge_continuation_rows(rows)
    rows = [r for r in rows if any(c for c in r)]

    ncols = len(cuts) + 1
    if ncols < 2 or len(rows) < 2:
        return None
    total = ncols * len(rows)
    nonempty = sum(1 for r in rows for c in r if c)
    if nonempty / total < 0.45:
        return None
    return rows


def lattice_table(words: List[dict], col_xs: List[float],
                  row_ys: List[float]) -> Optional[List[List[str]]]:
    """Rebuild a fully ruled table given detected grid line positions
    (used by the OCR path, where lines come from pixel analysis)."""
    col_xs = sorted(col_xs)
    row_ys = sorted(row_ys)
    if len(col_xs) < 2 or len(row_ys) < 2:
        return None
    nrows, ncols = len(row_ys) - 1, len(col_xs) - 1
    grid = [[[] for _ in range(ncols)] for _ in range(nrows)]
    for w in words:
        cx = (w["x0"] + w["x1"]) / 2.0
        cy = (w["top"] + w["bottom"]) / 2.0
        ri = ci = None
        for i in range(nrows):
            if row_ys[i] <= cy <= row_ys[i + 1]:
                ri = i
                break
        for j in range(ncols):
            if col_xs[j] <= cx <= col_xs[j + 1]:
                ci = j
                break
        if ri is not None and ci is not None:
            grid[ri][ci].append(w)
        else:
            return None  # never consume a region while dropping its words
    rows = []
    for r in grid:
        cells = []
        for cell_words in r:
            cell_words.sort(key=lambda w: (round(w["top"], 1), w["x0"]))
            cells.append(clean_text(" ".join(w["text"] for w in cell_words)))
        rows.append(cells)
    rows = [r for r in rows if any(c for c in r)]
    return rows if len(rows) >= 2 else None
