"""Deterministic PDF -> Markdown conversion engine.

Every step is mechanical geometry and typography analysis -- no ML
inference beyond the deterministic Tesseract OCR engine for scanned
pages, no network calls, no randomness.  The same input file with the
same tool version always produces the same output file.

Pipeline per page (vector path -- pages with a text layer):
  1. Detect ruled tables (pdfplumber lattice detection).
  2. Detect borderless "booktabs" tables (horizontal rules + column
     alignment; see tables.py) -- the dominant style in scientific
     journals.
  3. Detect sidebars: filled background rectangles containing text.
  4. Rebuild lines from words, detect one- vs two-column layouts, and
     order all elements into natural reading order, placing tables and
     sidebars inline where they occur.
  5. Classify headings and list items by font size / leading glyphs.
  6. Render GitHub-flavoured Markdown.

Pages with no text layer (scanned papers) go through the OCR path
(ocr.py): render at 300 dpi, deskew, Tesseract word boxes, pixel-level
rule detection, then the exact same table reconstruction and layout
pipeline.  OCR'd pages are marked with an HTML comment so downstream
consumers know the provenance.

Document metadata (Info dictionary, XMP, producer strings, etc.) is
never read into the output -- only page content is emitted.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, List, Optional
from urllib.parse import quote

import pdfplumber

from .fileio import atomic_output, ensure_distinct_paths

from .figures import (
    bind_caption,
    caption_label,
    detect_figure_regions,
    extract_image,
)
from .equations import (
    bare_tag,
    equation_tag,
    is_equation_line,
    render_equation,
    render_latex,
)
from .geometry import Line, clean_text, order_items, words_to_lines
from .mathparse import bars_in_region, reconstruct
from .sidebars import (
    block_bbox,
    detect_boxed_regions,
    detect_typographic_blocks,
    dominant_body_size,
)
from .ocr import (
    OcrOptions,
    ocr_available,
    ocr_page,
    page_needs_ocr,
    tesseract_version,
)
from .tables import (
    find_h_rules_vector,
    group_rule_regions,
    lattice_table,
    reconstruct_table,
)

BULLET_RE = re.compile(
    "^([•◦·‣⁃∙*]"
    r"|\(cid:\d+\)"
    "|[-–—](?=\\s))\\s*"
)
NUMBERED_RE = re.compile(r"^(\d{1,3})[.)]\s+")

# A candidate figure region holding more words than this is text
# over artwork, not a figure; it is discarded and the words stay.
FIGURE_MAX_WORDS = 40


# --------------------------------------------------------------------------
# data model
# --------------------------------------------------------------------------

@dataclass
class TableItem:
    rows: List[List[str]]
    x0: float
    x1: float
    top: float
    bottom: float
    kind: str = "table"


@dataclass
class SidebarItem:
    lines: List[Line] = field(default_factory=list)
    x0: float = 0.0
    x1: float = 0.0
    top: float = 0.0
    bottom: float = 0.0
    kind: str = "sidebar"


@dataclass
class FigureItem:
    """An image or vector illustration, with its caption if one was found."""
    x0: float
    x1: float
    top: float
    bottom: float
    page_number: int = 0
    caption: Optional[str] = None
    label: Optional[str] = None
    href: Optional[str] = None
    kind: str = "figure"


@dataclass
class EquationItem:
    """A displayed equation: verbatim glyphs, plus LaTeX when it is certain."""
    verbatim: List[str]
    x0: float
    x1: float
    top: float
    bottom: float
    tag: Optional[str] = None
    latex: Optional[str] = None
    kind: str = "equation"


@dataclass
class PageStream:
    """Ordered content items for one page."""
    items: list = field(default_factory=list)
    note: Optional[str] = None
    warnings: List[str] = field(default_factory=list)


def _center_in(bbox, x, y) -> bool:
    x0, top, x1, bottom = bbox
    return x0 <= x <= x1 and top <= y <= bottom


def _word_center(w):
    return ((w["x0"] + w["x1"]) / 2.0, (w["top"] + w["bottom"]) / 2.0)


# --------------------------------------------------------------------------
# vector path (pages with a text layer)
# --------------------------------------------------------------------------

def _extract_lattice_rows(table) -> List[List[str]]:
    rows = []
    for raw in table.extract():
        row = []
        for cell in raw:
            cell = "" if cell is None else str(cell)
            row.append(clean_text(re.sub(r"\s+", " ", cell)))
        rows.append(row)
    return [r for r in rows if any(c for c in r)]


def _words_in_region(words, region, pad_x=2.0, pad_y=3.0):
    x0, top, x1, bottom = region
    inside, outside = [], []
    for w in words:
        cx, cy = _word_center(w)
        if x0 - pad_x <= cx <= x1 + pad_x and top - pad_y <= cy <= bottom + pad_y:
            inside.append(w)
        else:
            outside.append(w)
    return inside, outside


def _parse_page_vector(page, page_number: int = 0,
                       assets=None,
                       reconstruct_math: bool = True) -> PageStream:
    """Parse a page that has a text layer.

    ``assets``, when given, is an ``(output_dir, href_prefix)`` pair;
    figures are then rendered to PNG files in that directory and
    linked from the Markdown.  Without it figures are marked with an
    HTML comment and their caption is kept in place.
    """
    stream = PageStream()

    # 1. ruled (lattice) tables
    tables = page.find_tables()
    table_bboxes = []
    table_items = []
    for t in tables:
        rows = _extract_lattice_rows(t)
        if rows:
            table_bboxes.append(t.bbox)
            x0, top, x1, bottom = t.bbox
            table_items.append(TableItem(rows, x0, x1, top, bottom))

    words = page.extract_words(extra_attrs=["size", "fontname"],
                               keep_blank_chars=False)
    words = [w for w in words
             if not any(_center_in(tb, *_word_center(w)) for tb in table_bboxes)]

    # 2. borderless (booktabs) tables between horizontal rules
    h_rules = find_h_rules_vector(page)
    for region in group_rule_regions(h_rules, skip_bboxes=table_bboxes):
        inside, outside = _words_in_region(words, region)
        rows = reconstruct_table(inside) if inside else None
        if rows:
            table_items.append(TableItem(rows, region[0], region[2],
                                         region[1], region[3]))
            table_bboxes.append(region)
            words = outside  # consumed

    # 3. sidebars
    sidebar_regions = detect_boxed_regions(page, table_bboxes)
    body_words = []
    sidebar_words: List[list] = [[] for _ in sidebar_regions]
    for w in words:
        cx, cy = _word_center(w)
        placed = False
        for i, region in enumerate(sidebar_regions):
            if _center_in(region, cx, cy):
                sidebar_words[i].append(w)
                placed = True
                break
        if not placed:
            body_words.append(w)

    sidebar_items = []
    for region, ws in zip(sidebar_regions, sidebar_words):
        if len(ws) < 4:
            body_words.extend(ws)  # decorative rect, not a sidebar
            continue
        lines = order_items(words_to_lines(ws), region[0], region[2])
        sidebar_items.append(SidebarItem(lines=lines, x0=region[0], x1=region[2],
                                         top=region[1], bottom=region[3]))

    # 4. figures: images and vector-drawing clusters outside tables/sidebars
    figure_regions = detect_figure_regions(
        page, exclude_bboxes=list(table_bboxes) + list(sidebar_regions))
    kept_regions = []
    for region in figure_regions:
        inside, outside = _words_in_region(body_words, region,
                                           pad_x=0.0, pad_y=0.0)
        if len(inside) > FIGURE_MAX_WORDS:
            continue  # text over artwork, not a figure -- leave it as body
        kept_regions.append(region)
        body_words = outside  # in-figure labels don't belong in the prose

    lines = list(words_to_lines(body_words))
    figure_items = _build_figures(page, page_number, kept_regions, lines, assets)
    lines = [ln for ln in lines if ln is not None]

    # 5. displayed equations, reconstructed from glyph geometry where certain
    lines, equation_items = _split_equations(
        page, lines,
        exclude_bboxes=list(table_bboxes) + list(sidebar_regions),
        reconstruct_math=reconstruct_math)

    # 6. unboxed callouts: inset, typographically distinct runs of lines
    lines, unboxed = _split_typographic_sidebars(lines)
    sidebar_items.extend(unboxed)

    # 7. reading order
    all_items = (lines + table_items + sidebar_items + figure_items
                 + equation_items)
    if all_items:
        content_x0 = min(it.x0 for it in all_items)
        content_x1 = max(it.x1 for it in all_items)
        stream.items = order_items(all_items, content_x0, content_x1)
    return stream


EQUATION_PAD = 2.0


FRACTION_REACH = 200.0      # pt; how far from a bar an equation may extend


def _fraction_bands(page, lines, exclude_bboxes):
    """Vertical bands occupied by a fraction (a rule with text above and below).

    A fraction built from ordinary letters carries no mathematical glyphs
    at all, so the typographic signal cannot see it; the rule itself is
    the evidence.
    """
    bands = []
    max_bar = 0.6 * float(page.width)
    for bar in bars_in_region(page, (0, 0, page.width, page.height)):
        if bar.width > max_bar:
            continue                      # a full-width rule is a separator
        if any(bx0 <= bar.cx <= bx1 and btop <= bar.y <= bbot
               for bx0, btop, bx1, bbot in exclude_bboxes):
            continue
        near = [ln for ln in lines
                if ln.x0 >= bar.x0 - 4 and ln.x1 <= bar.x1 + 4
                and abs((ln.top + ln.bottom) / 2 - bar.y)
                <= 3 * (ln.bottom - ln.top)]
        above = [ln for ln in near if ln.bottom <= bar.y + 1]
        below = [ln for ln in near if ln.top >= bar.y - 1]
        if not above or not below:
            continue                      # an overline or an underline, not a
        bands.append((bar, min(ln.top for ln in near),   # fraction
                      max(ln.bottom for ln in near)))
    return bands


def _in_band(line, band) -> bool:
    bar, top, bottom = band
    if line.bottom < top - 1 or line.top > bottom + 1:
        return False
    return abs((line.x0 + line.x1) / 2 - bar.cx) <= FRACTION_REACH


def _split_equations(page, lines, exclude_bboxes=(),
                     reconstruct_math: bool = True):
    """Pull runs of equation lines out of the body into EquationItems."""
    ordered = sorted(lines, key=lambda ln: (round(ln.top, 1), ln.x0))
    bands = _fraction_bands(page, ordered, exclude_bboxes)

    def is_equation(ln) -> bool:
        return (is_equation_line(ln.text, ln.math)
                or any(_in_band(ln, b) for b in bands))

    items, taken = [], set()
    i = 0
    while i < len(ordered):
        if id(ordered[i]) in taken or not is_equation(ordered[i]):
            i += 1
            continue
        run, tag, label_line = [], None, None
        while (i < len(ordered) and id(ordered[i]) not in taken
               and is_equation(ordered[i])
               and (not run or (
                   ordered[i].top - run[-1].bottom <= max(ordered[i].size, run[-1].size)
                   and (min(ordered[i].x1, run[-1].x1) >= max(ordered[i].x0, run[-1].x0)
                        or any(_in_band(ordered[i], b) and _in_band(run[-1], b)
                               for b in bands))))):
            run.append(ordered[i])
            tag = tag or equation_tag(ordered[i].text)
            i += 1
        # a right-aligned "(3)" beside the equation is its number, so it
        # belongs to the equation's region but not to its content
        if i < len(ordered) and tag is None:
            nxt = ordered[i]
            if bare_tag(nxt.text) and nxt.top < run[-1].bottom \
                    and nxt.bottom > run[-1].top:
                tag = bare_tag(nxt.text)
                label_line = nxt
                run.append(nxt)
                i += 1
        run = _absorb_stacked(run, ordered, taken)
        region = (min(l.x0 for l in run) - EQUATION_PAD,
                  min(l.top for l in run) - EQUATION_PAD,
                  max(l.x1 for l in run) + EQUATION_PAD,
                  max(l.bottom for l in run) + EQUATION_PAD)
        taken.update(id(l) for l in run)
        items.append(EquationItem(
            verbatim=[l.text for l in run if l is not label_line],
            x0=region[0], top=region[1], x1=region[2], bottom=region[3],
            tag=tag,
            latex=(_reconstruct_region(page, region)
                   if reconstruct_math else None)))
    remaining = [ln for ln in lines if id(ln) not in taken]
    return remaining, items


STACK_GROW = 1.2            # line heights of headroom for limits and scripts


def _absorb_stacked(run, ordered, taken):
    """Take in material stacked above or below the equation.

    An operator's limits and a fraction's halves are separate visual
    lines, and on their own they look like ordinary short lines.  They
    are pulled in when they sit inside the equation's own column -- a
    body paragraph runs to the margin and so cannot be absorbed.
    """
    height = max((l.bottom - l.top) for l in run)
    x0 = min(l.x0 for l in run) - EQUATION_PAD
    x1 = max(l.x1 for l in run) + EQUATION_PAD
    top = min(l.top for l in run) - STACK_GROW * height
    bottom = max(l.bottom for l in run) + STACK_GROW * height
    inside = [ln for ln in ordered
              if ln not in run and id(ln) not in taken
              and ln.x0 >= x0 - EQUATION_PAD and ln.x1 <= x1 + EQUATION_PAD
              and ln.top >= top and ln.bottom <= bottom]
    return sorted(run + inside, key=lambda l: (round(l.top, 1), l.x0))


def _reconstruct_region(page, region) -> Optional[str]:
    """LaTeX for an equation region, or None when it isn't certain."""
    x0, top, x1, bottom = region
    chars = [ch for ch in page.chars
             if x0 <= (ch["x0"] + ch["x1"]) / 2 <= x1
             and top <= (ch["top"] + ch["bottom"]) / 2 <= bottom]
    if not chars:
        return None
    # the equation number is a label on the equation, not part of it
    label_start = _label_start(chars)
    if label_start is not None:
        chars = [ch for ch in chars if ch["x1"] <= label_start]
    if not chars:
        return None
    return reconstruct(chars, bars_in_region(page, region))


def _label_start(chars) -> Optional[float]:
    """x where a trailing "(3)" label begins, if the row ends with one."""
    ordered = sorted(chars, key=lambda ch: ch["x0"])
    text, owner = "", []
    for ch in ordered:                     # a glyph can decode to several
        piece = ch["text"] or ""           # characters, so map back by index
        text += piece
        owner.extend([ch] * len(piece))
    m = re.search(r"[\(\[][^()\[\]]{1,6}[\)\]]\s*$", text)
    if not m or m.start() >= len(owner):
        return None
    start = owner[m.start()]
    prev = owner[m.start() - 1] if m.start() > 0 else None
    if prev is None or start["x0"] - prev["x1"] < 12:
        return None                        # a label is set clear of the maths
    return start["x0"] - 0.5


def _split_typographic_sidebars(lines):
    """Pull unboxed callout blocks out of the body lines into sidebars."""
    body_size = dominant_body_size(lines)
    if body_size is None:
        return lines, []
    ordered = sorted(lines, key=lambda ln: (round(ln.top, 1), ln.x0))
    blocks = detect_typographic_blocks(ordered, float(body_size))
    if not blocks:
        return lines, []
    taken = set()
    items = []
    for start, end in blocks:
        run = ordered[start:end]
        taken.update(id(ln) for ln in run)
        x0, top, x1, bottom = block_bbox(run)
        items.append(SidebarItem(lines=run, x0=x0, x1=x1,
                                 top=top, bottom=bottom))
    remaining = [ln for ln in lines if id(ln) not in taken]
    return remaining, items


def _build_figures(page, page_number, regions, lines, assets) -> List[FigureItem]:
    """Make FigureItems, consuming caption lines out of ``lines`` in place."""
    items = []
    for n, region in enumerate(regions, start=1):
        idx = bind_caption(region, [ln for ln in lines if ln is not None])
        caption = label = None
        if idx is not None:
            present = [i for i, ln in enumerate(lines) if ln is not None]
            real = present[idx]
            caption = lines[real].text
            label = caption_label(caption)
            lines[real] = None  # consumed: never repeat it as a paragraph
        href = None
        if assets is not None:
            outdir, prefix = assets
            name = f"p{page_number}-fig{n}.png"
            os.makedirs(outdir, exist_ok=True)
            try:
                extract_image(page, region, os.path.join(outdir, name))
                href = f"{prefix}/{name}" if prefix else name
            except Exception:
                href = None  # rendering unavailable; fall back to a marker
        items.append(FigureItem(
            x0=region[0], x1=region[2], top=region[1], bottom=region[3],
            page_number=page_number, caption=caption, label=label, href=href))
    return items


# --------------------------------------------------------------------------
# OCR path (scanned pages)
# --------------------------------------------------------------------------

MAX_LISTED_SUSPECTS = 12


def _suspect_note(page_number: int, words) -> Optional[str]:
    """One comment listing the numeric tokens a reader should check."""
    flagged = [w for w in words if w.get("suspect")]
    if not flagged:
        return None
    shown = flagged[:MAX_LISTED_SUSPECTS]
    listed = ", ".join(f"{w['text']!r} ({w['suspect']})" for w in shown)
    listed = listed.replace("--", "&#45;&#45;").replace("<", "&lt;").replace(">", "&gt;")
    more = ("" if len(flagged) == len(shown)
            else f", and {len(flagged) - len(shown)} more")
    n = len(flagged)
    return (f"<!-- page {page_number}: {n} numeric token"
            f"{'' if n == 1 else 's'} to check against the source: "
            f"{listed}{more} -->")


def _parse_page_ocr(page, page_number: int,
                    options: Optional[OcrOptions] = None) -> PageStream:
    options = options or OcrOptions()
    stream = PageStream()
    version = tesseract_version() or "unknown version"
    stream.note = (f"<!-- page {page_number}: no text layer; converted with "
                   f"OCR (tesseract {version}, psm {options.psm}, "
                   f"{options.dpi} dpi, lang {options.lang}) -->")

    words, h_rules, v_rules = ocr_page(page, options)
    suspects = _suspect_note(page_number, words)
    if suspects:
        stream.warnings.append(suspects)
    if not words:
        return stream

    table_items = []
    for region in group_rule_regions(h_rules):
        inside, outside = _words_in_region(words, region, pad_y=1.0)
        if not inside:
            continue
        x0, top, x1, bottom = region
        interior_v = [
            v for v in v_rules
            if x0 - 2 <= v["x"] <= x1 + 2
            and (min(v["bottom"], bottom) - max(v["top"], top))
                >= 0.5 * (v["bottom"] - v["top"])
        ]
        region_h = sorted(r["top"] for r in h_rules
                          if top - 1 <= r["top"] <= bottom + 1)
        rows = None
        if len(interior_v) >= 1 and len(region_h) >= 3:
            col_xs = [x0 - 2] + sorted(v["x"] for v in interior_v) + [x1 + 2]
            rows = lattice_table(inside, col_xs, region_h)
        if rows is None:
            rows = reconstruct_table(inside)
        if rows:
            table_items.append(TableItem(rows, x0, x1, top, bottom))
            words = outside

    all_items = list(words_to_lines(words)) + table_items
    if all_items:
        content_x0 = min(it.x0 for it in all_items)
        content_x1 = max(it.x1 for it in all_items)
        stream.items = order_items(all_items, content_x0, content_x1)
    return stream


# --------------------------------------------------------------------------
# markdown rendering
# --------------------------------------------------------------------------

def _escape_cell(s: str) -> str:
    return re.sub(r"\s+", " ", s).replace("\\", "\\\\").replace("|", "\\|")


def _render_table(rows: List[List[str]]) -> str:
    if not rows:
        return ""
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    out = ["| " + " | ".join(_escape_cell(c) for c in rows[0]) + " |"]
    out.append("|" + "|".join(" --- " for _ in range(ncols)) + "|")
    for r in rows[1:]:
        out.append("| " + " | ".join(_escape_cell(c) for c in r) + " |")
    return "\n".join(out)


def _render_figure(fig) -> List[str]:
    """A figure becomes an image link, or a marker plus its caption."""
    alt = fig.caption or fig.label or "Figure"
    if fig.href:
        return ["![" + _escape_link_text(alt) + "](" + quote(fig.href, safe="/") + ")"]
    w = fig.x1 - fig.x0
    h = fig.bottom - fig.top
    marker = (f"<!-- figure: page {fig.page_number}, "
              f"{w:.0f}x{h:.0f}pt at ({fig.x0:.0f},{fig.top:.0f}); "
              f"image not extracted -->")
    blocks = [marker]
    if fig.caption:
        blocks.append("*" + fig.caption + "*")
    return blocks


def _escape_link_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _join_par(lines: List[str]) -> str:
    """Join wrapped lines into one paragraph, repairing hyphenation."""
    out = ""
    for piece in lines:
        if not out:
            out = piece
            continue
        if out.endswith("-") and piece[:1].islower():
            out = out[:-1] + piece
        else:
            out += " " + piece
    return re.sub(r"\s+", " ", out).strip()


class _Renderer:
    def __init__(self, body_size: float, heading_sizes: List[float]):
        self.body_size = body_size
        self.heading_sizes = heading_sizes
        self.blocks: List[str] = []

    def _heading_level(self, size: float) -> int:
        rounded = round(size * 2) / 2
        try:
            return min(self.heading_sizes.index(rounded) + 1, 6)
        except ValueError:
            return min(len(self.heading_sizes) + 1, 6)

    def _is_heading(self, line: Line) -> bool:
        return (
            line.size >= self.body_size * 1.18
            and len(line.text.split()) <= 14
        )

    def render_lines(self, lines: List[Line], quote: bool = False) -> List[str]:
        """Fold ordered lines into markdown blocks."""
        blocks: List[str] = []
        par: List[str] = []
        par_last: Optional[Line] = None

        def flush():
            nonlocal par, par_last
            if par:
                text = _join_par(par)
                if text:
                    blocks.append(text)
            par = []
            par_last = None

        i = 0
        while i < len(lines):
            ln = lines[i]
            text = ln.text
            if not text:
                i += 1
                continue

            if self._is_heading(ln):
                flush()
                # merge immediately-following heading lines of the same size
                htexts = [text]
                j = i + 1
                while (
                    j < len(lines)
                    and self._is_heading(lines[j])
                    and abs(lines[j].size - ln.size) < 0.6
                    and lines[j].top >= lines[j - 1].top
                    and lines[j].top - lines[j - 1].bottom < ln.size
                ):
                    htexts.append(lines[j].text)
                    j += 1
                level = self._heading_level(round(ln.size * 2) / 2)
                blocks.append("#" * level + " " + _join_par(htexts))
                i = j
                continue

            m = BULLET_RE.match(text)
            n = NUMBERED_RE.match(text)
            if m or n:
                flush()
                items = []
                while i < len(lines):
                    if items and (self._is_heading(lines[i])
                                  or lines[i].top < lines[i - 1].top - 2):
                        break
                    lt = lines[i].text
                    bm = BULLET_RE.match(lt)
                    nm = NUMBERED_RE.match(lt)
                    if bm:
                        items.append(("- ", bm.end(), i))
                    elif nm:
                        items.append((nm.group(1) + ". ", nm.end(), i))
                    elif items and lines[i].top - lines[i - 1].bottom < lines[i].size:
                        pass  # wrapped continuation; absorbed in _render_list
                    else:
                        break
                    i += 1
                blocks.extend(self._render_list(lines[:i], items))
                continue

            gap_break = (
                par_last is not None
                and (ln.top - par_last.bottom) > max(ln.size, par_last.size) * 0.9
            )
            col_break = (
                par_last is not None
                and ln.top < par_last.top - 2  # jumped back up: new column
            )
            if gap_break or col_break:
                flush()
            par.append(text)
            par_last = ln
            i += 1

        flush()
        if quote:
            blocks = ["\n".join("> " + l for l in b.split("\n")) for b in blocks]
        return blocks

    def _render_list(self, lines: List[Line], items) -> List[str]:
        """Render collected list-item line indices as a markdown list."""
        out = []
        for k, (marker, end, idx) in enumerate(items):
            txt = lines[idx].text[end:].strip()
            # absorb wrapped continuation lines up to the next item
            nxt = items[k + 1][2] if k + 1 < len(items) else None
            j = idx + 1
            while j < len(lines) and (nxt is None or j < nxt):
                cont = lines[j]
                if BULLET_RE.match(cont.text) or NUMBERED_RE.match(cont.text):
                    break
                if cont.top - lines[j - 1].bottom < cont.size:
                    txt = _join_par([txt, cont.text])
                    j += 1
                else:
                    break
            out.append(marker + txt)
        return ["\n".join(out)] if out else []

    def render_stream(self, stream: PageStream) -> None:
        if stream.note:
            self.blocks.append(stream.note)
        self.blocks.extend(stream.warnings)

        pending_lines: List[Line] = []

        def flush_lines():
            nonlocal pending_lines
            if pending_lines:
                self.blocks.extend(self.render_lines(pending_lines))
                pending_lines = []

        for it in stream.items:
            if it.kind == "line":
                pending_lines.append(it)
            elif it.kind == "table":
                flush_lines()
                self.blocks.append(_render_table(it.rows))
            elif it.kind == "sidebar":
                flush_lines()
                quoted = self.render_lines(it.lines, quote=True)
                if quoted:
                    self.blocks.append("\n>\n".join(quoted))
            elif it.kind == "figure":
                flush_lines()
                self.blocks.extend(_render_figure(it))
            elif it.kind == "equation":
                flush_lines()
                if it.latex:
                    self.blocks.append(render_latex(it.latex, it.tag))
                else:
                    self.blocks.append(render_equation(it.verbatim, it.tag))
        flush_lines()


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def convert_pdf_to_markdown(
    pdf_path: str,
    progress: Optional[Callable[[int, int], None]] = None,
    assets_dir: Optional[str] = None,
    assets_href: Optional[str] = None,
    ocr_options: Optional[OcrOptions] = None,
    reconstruct_math: bool = True,
) -> str:
    """Convert one PDF file to a Markdown string (content only).

    When ``assets_dir`` is given, figures are rendered to PNG files in
    that directory and linked from the Markdown using ``assets_href`` as
    the link prefix (which should be relative to wherever the Markdown
    will live).  Otherwise each figure is marked with an HTML comment and
    its caption is emitted in place.
    """
    assets = (assets_dir, assets_href) if assets_dir else None
    ocr_options = ocr_options or OcrOptions()
    streams: List[PageStream] = []
    with pdfplumber.open(pdf_path) as pdf:
        npages = len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            if page_needs_ocr(page):
                if ocr_available():
                    streams.append(
                        _parse_page_ocr(page, i + 1, ocr_options))
                else:
                    st = PageStream()
                    st.note = (f"<!-- page {i + 1}: scanned image, no text "
                               f"layer; OCR engine (Tesseract) not found, "
                               f"page skipped -->")
                    streams.append(st)
            else:
                streams.append(_parse_page_vector(
                    page, i + 1, assets,
                    reconstruct_math=reconstruct_math))
            page.close()  # release cached glyphs/images before the next page
            if progress:
                progress(i + 1, npages)

    # global typography: dominant body size weighted by character count
    size_counter: Counter = Counter()
    for st in streams:
        for it in st.items:
            if it.kind == "line":
                size_counter[round(it.size * 2) / 2] += it.nchars
            elif it.kind == "sidebar":
                for ln in it.lines:
                    size_counter[round(ln.size * 2) / 2] += ln.nchars
    body_size = size_counter.most_common(1)[0][0] if size_counter else 12.0

    heading_sizes = sorted(
        {s for s in size_counter if s >= body_size * 1.18},
        reverse=True,
    )

    renderer = _Renderer(body_size=float(body_size), heading_sizes=heading_sizes)
    for st in streams:
        renderer.render_stream(st)

    md = "\n\n".join(b for b in renderer.blocks if b.strip())
    return md + "\n" if md else ""


def convert_file(pdf_path: str, out_path: str,
                 progress: Optional[Callable[[int, int], None]] = None,
                 extract_images: bool = False,
                 ocr_options: Optional[OcrOptions] = None,
                 reconstruct_math: bool = True) -> str:
    """Convert ``pdf_path`` and write the Markdown to ``out_path``.

    With ``extract_images``, figures are written as PNGs into a sibling
    ``<name>_assets/`` directory and linked from the Markdown.
    """
    ensure_distinct_paths(pdf_path, out_path)
    assets_dir = assets_href = None
    if extract_images:
        stem = os.path.splitext(os.path.basename(out_path))[0]
        assets_href = stem + "_assets"
        assets_dir = os.path.join(
            os.path.dirname(os.path.abspath(out_path)), assets_href)
    md = convert_pdf_to_markdown(pdf_path, progress=progress,
                                 assets_dir=assets_dir,
                                 assets_href=assets_href,
                                 ocr_options=ocr_options,
                                 reconstruct_math=reconstruct_math)
    with atomic_output(out_path) as f:
        f.write(md.encode("utf-8"))
    return out_path
