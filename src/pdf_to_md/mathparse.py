"""Reconstruct LaTeX from the geometry of an equation's glyphs.

A PDF records where each glyph sits, not what the expression means, so
structure has to be read back out of the layout.  This is the classical
baseline-structure recursion:

  * a **fraction** is a horizontal rule with material above and below it;
    each side is parsed independently;
  * a **radical** is a root glyph followed by an overline; what the
    overline covers is the radicand;
  * a **big operator** (sum, product, integral) carries limits centred
    above and below it;
  * everything else sits on a baseline, and a glyph that is smaller and
    raised (or dropped) relative to that baseline is a superscript (or
    subscript), parsed recursively in turn.

Everything here is geometry, so it is deterministic.

The parser refuses rather than guesses.  ``reconstruct`` returns None
when any glyph cannot be identified, when a rule on the page is left
unexplained, or when the layout does not resolve cleanly -- and the
caller then falls back to emitting the raw glyph run.  A plainly raw
equation is honest; a plausible-looking wrong one is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

from .mathsymbols import BIG_OPERATORS, FUNCTION_NAMES, to_latex, to_unicode

SCRIPT_SIZE_RATIO = 0.92   # a script is set smaller than its base
SCRIPT_RISE = 0.18         # of base size, before a glyph counts as raised
BAR_MAX_THICKNESS = 3.0    # pt; thicker rules are not fraction bars
BAR_MIN_WIDTH = 4.0        # pt
FRACTION_COVERAGE = 0.55   # a fraction bar spans most of its expression
LIMIT_SLACK = 0.55         # of operator width, for limits set off to the side
MAX_DEPTH = 12             # guards against pathological nesting
BASE_SPAN_SHARE = 0.4      # of the widest level, to count as running


@dataclass
class Glyph:
    """One positioned character, or a sub-expression already rendered."""
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    latex: Optional[str] = None   # set for a rendered sub-expression
    anchor: bool = False          # sub-expressions always sit on the line

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0


@dataclass
class Bar:
    """A horizontal rule: a fraction bar or a radical's overline."""
    x0: float
    x1: float
    y: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2.0


class Unreconstructable(Exception):
    """Raised as soon as the layout stops being certain."""


# --------------------------------------------------------------------------
# input preparation
# --------------------------------------------------------------------------

def glyphs_from_chars(chars: Sequence[dict]) -> Optional[List[Glyph]]:
    """Resolve extracted chars to Unicode glyphs; None if any is unknown."""
    out = []
    for ch in chars:
        text = (ch.get("text") or "")
        if not text.strip():
            continue
        uni = to_unicode(text, ch.get("fontname"))
        if uni is None:
            return None
        for c in uni:
            if not c.strip():
                continue
            out.append(Glyph(text=c, x0=float(ch["x0"]), x1=float(ch["x1"]),
                             top=float(ch["top"]), bottom=float(ch["bottom"]),
                             size=float(ch.get("size") or
                                        (ch["bottom"] - ch["top"]))))
    return sorted(out, key=lambda g: (g.x0, g.top))


def bars_in_region(page, region) -> List[Bar]:
    """Thin horizontal rules inside ``region`` (fraction bars, overlines)."""
    x0, top, x1, bottom = region
    bars = []
    candidates = list(page.lines) + [r for r in page.rects if r.get("fill")]
    for obj in candidates:
        h = abs(obj["bottom"] - obj["top"])
        w = obj["x1"] - obj["x0"]
        if h > BAR_MAX_THICKNESS or w < BAR_MIN_WIDTH:
            continue
        y = (obj["top"] + obj["bottom"]) / 2.0
        if (x0 - 2 <= obj["x0"] and obj["x1"] <= x1 + 2
                and top - 2 <= y <= bottom + 2):
            bars.append(Bar(float(obj["x0"]), float(obj["x1"]), float(y)))
    return sorted(bars, key=lambda b: (b.y, b.x0))


# --------------------------------------------------------------------------
# recursion
# --------------------------------------------------------------------------

def _span(glyphs: Sequence[Glyph]):
    return (min(g.x0 for g in glyphs), max(g.x1 for g in glyphs))


def _base_size(glyphs: Sequence[Glyph]) -> float:
    """The size of the expression's own level.

    Neither the largest size present (a big operator is deliberately set
    larger than what it operates on) nor the commonest (a formula can
    carry more script glyphs than base ones).  The base level is the one
    that runs the width of the expression; scripts and limits are local.
    """
    by_size: dict = {}
    for g in glyphs:
        by_size.setdefault(round(g.size * 2) / 2, []).append(g)
    if len(by_size) == 1:
        return next(iter(by_size))

    def span(group):
        return max(g.x1 for g in group) - min(g.x0 for g in group)

    spans = {size: span(group) for size, group in by_size.items()}
    widest = max(spans.values())
    if widest <= 0:
        sizes = sorted(g.size for g in glyphs)
        return sizes[len(sizes) // 2]
    # Among the levels that actually run across the expression, the base is
    # the largest: scripts are both smaller and more local, while a big
    # operator is larger but occupies almost no width.
    running = [size for size, sp in spans.items() if sp >= BASE_SPAN_SHARE * widest]
    return max(running) if running else max(spans, key=spans.get)


def _under(bar: Bar, glyphs: Sequence[Glyph]) -> List[Glyph]:
    return [g for g in glyphs
            if bar.x0 - 1 <= g.cx <= bar.x1 + 1 and g.top >= bar.y - 1]


def _over(bar: Bar, glyphs: Sequence[Glyph]) -> List[Glyph]:
    return [g for g in glyphs
            if bar.x0 - 1 <= g.cx <= bar.x1 + 1 and g.bottom <= bar.y + 1]


def _expression(glyphs: List[Glyph], bars: List[Bar], depth: int = 0) -> str:
    """LaTeX for a set of glyphs, splitting on the outermost fraction bar."""
    if depth > MAX_DEPTH:
        raise Unreconstructable("nesting too deep")
    if not glyphs:
        if bars:
            raise Unreconstructable("rule without glyphs")
        return ""

    bar = _dominant_bar(glyphs, bars)
    if bar is not None:
        return _fraction(bar, glyphs, bars, depth)
    return _row(glyphs, list(bars), depth)


def _dominant_bar(glyphs: Sequence[Glyph],
                  bars: Sequence[Bar]) -> Optional[Bar]:
    """A bar spanning most of the expression, with material on both sides."""
    if not bars:
        return None
    x0, x1 = _span(glyphs)
    width = max(x1 - x0, 1.0)
    candidates = [b for b in bars
                  if b.width >= FRACTION_COVERAGE * width
                  and _over(b, glyphs) and _under(b, glyphs)]
    return max(candidates, key=lambda b: b.width) if candidates else None


def _fraction(bar: Bar, glyphs: Sequence[Glyph], bars: Sequence[Bar],
              depth: int) -> str:
    above, below = _over(bar, glyphs), _under(bar, glyphs)
    if len(above) + len(below) != len(glyphs):
        raise Unreconstructable("glyphs straddle a fraction bar")
    rest = [b for b in bars if b is not bar]
    if any(b.y == bar.y for b in rest):
        raise Unreconstructable("unexplained coincident fraction rule")
    num = _expression(above, [b for b in rest if b.y < bar.y], depth + 1)
    den = _expression(below, [b for b in rest if b.y > bar.y], depth + 1)
    return "\\frac{" + num + "}{" + den + "}"


def _extract_fractions(glyphs: List[Glyph], bars: List[Bar], depth: int):
    """Replace each embedded fraction with a single rendered unit."""
    units: List[Glyph] = []
    remaining = list(glyphs)
    left = []
    for bar in sorted(bars, key=lambda b: -b.width):
        if bar not in bars:
            continue  # already consumed by an enclosing fraction
        above, below = _over(bar, remaining), _under(bar, remaining)
        if not above or not below:
            left.append(bar)          # an overline, or nothing to divide
            continue
        inner_bars = [b for b in bars
                      if b is not bar and bar.x0 - 1 <= b.cx <= bar.x1 + 1]
        if any(b.y == bar.y for b in inner_bars):
            raise Unreconstructable("unexplained coincident fraction rule")
        num = _expression(above, [b for b in inner_bars if b.y < bar.y],
                          depth + 1)
        den = _expression(below, [b for b in inner_bars if b.y > bar.y],
                          depth + 1)
        used = {id(g) for g in above + below}
        remaining = [g for g in remaining if id(g) not in used]
        bars = [b for b in bars if b is bar or b not in inner_bars]
        size = max((g.size for g in above + below), default=10.0)
        units.append(Glyph(text="", x0=bar.x0, x1=bar.x1,
                           top=min(g.top for g in above),
                           bottom=bar.y, size=size,
                           latex="\\frac{" + num + "}{" + den + "}",
                           anchor=True))
    return remaining, units, left


def _row(glyphs: List[Glyph], bars: List[Bar], depth: int) -> str:
    """One baseline: units left to right, with scripts attached."""
    glyphs, fractions, bars = _extract_fractions(glyphs, bars, depth)
    if not glyphs and not fractions:
        raise Unreconstructable("nothing to render")

    if glyphs:
        base = _base_size(glyphs)
        full = [g for g in glyphs if g.size >= base * SCRIPT_SIZE_RATIO]
        baseline = sorted(g.bottom for g in full)[len(full) // 2] if full \
            else sorted(g.bottom for g in glyphs)[len(glyphs) // 2]
    else:
        base = max(f.size for f in fractions)
        baseline = sorted(f.bottom for f in fractions)[len(fractions) // 2]
    rise = SCRIPT_RISE * base

    # Size decides, not box position: glyph boxes from different fonts sit
    # at different heights on the same baseline (a Symbol operator and a
    # Times letter differ by a couple of points), but a script is always
    # set smaller than what it modifies.
    def is_script(g: Glyph) -> bool:
        return (g.size < base * SCRIPT_SIZE_RATIO
                and abs(g.bottom - baseline) > rise * 0.5)

    main = [g for g in glyphs if not is_script(g)]
    others = [g for g in glyphs if is_script(g)]
    main = sorted(main + fractions, key=lambda g: g.x0)
    if not main:
        raise Unreconstructable("no baseline could be established")

    pieces: List[str] = []
    piece_of: dict = {}          # index in ``main`` -> index in ``pieces``
    consumed: set = set()        # glyphs eaten by a radical
    i = 0
    while i < len(main):
        g = main[i]
        if id(g) in consumed:
            i += 1
            continue

        if g.latex is not None:
            piece_of[i] = len(pieces)
            pieces.append(g.latex)
            i += 1
            continue

        if g.text == "\u221a":
            over = _overline_for(g, bars)
            if over is None:
                raise Unreconstructable("radical without an overline")
            inner = [h for h in main + others
                     if h is not g and id(h) not in consumed
                     and over.x0 - 1 <= h.cx <= over.x1 + 1
                     and h.top >= over.y - 1]
            if not inner:
                raise Unreconstructable("empty radicand")
            consumed.update(id(h) for h in inner)
            others = [h for h in others if id(h) not in consumed]
            bars = [b for b in bars if b is not over]
            piece_of[i] = len(pieces)
            pieces.append("\\sqrt{" + _expression(inner, [], depth + 1) + "}")
            i += 1
            continue

        if g.text in BIG_OPERATORS:
            lo, hi, eaten = _limits(g, others)
            others = [h for h in others if id(h) not in eaten]
            token = to_latex(g.text)
            if token is None:
                raise Unreconstructable("unknown operator")
            if lo:
                token += "_{" + _expression(lo, [], depth + 1) + "}"
            if hi:
                token += "^{" + _expression(hi, [], depth + 1) + "}"
            piece_of[i] = len(pieces)
            pieces.append(token)
            i += 1
            continue

        name, length = _function_name(main, i)
        if name:
            for k in range(i, i + length):
                piece_of[k] = len(pieces)
            pieces.append("\\" + name)
            i += length
            continue

        number, length = _number(main, i)
        if number:
            for k in range(i, i + length):
                piece_of[k] = len(pieces)
            pieces.append(number)
            i += length
            continue

        token = to_latex(g.text)
        if token is None:
            raise Unreconstructable("unknown glyph")
        piece_of[i] = len(pieces)
        pieces.append(token)
        i += 1

    for piece_index, kind, group in _scripts(main, others, piece_of,
                                             baseline, base):
        marker = "^" if kind == "sup" else "_"
        pieces[piece_index] += marker + "{" \
            + _expression(group, [], depth + 1) + "}"

    if bars:
        raise Unreconstructable("unexplained rule in the expression")
    return " ".join(p for p in pieces if p)


def _overline_for(radical: Glyph, bars: Sequence[Bar]) -> Optional[Bar]:
    for b in bars:
        if b.x0 >= radical.x0 - 2 \
                and abs(b.y - radical.top) <= radical.size * 0.5:
            return b
    return None


def _cluster(glyphs: Sequence[Glyph]) -> List[List[Glyph]]:
    """Group glyphs into the visual runs they were set as.

    A limit like "i=1" is three glyphs on one small baseline; treating
    them separately would leave the outer ones stranded.
    """
    groups: List[List[Glyph]] = []
    for g in sorted(glyphs, key=lambda g: (round(g.bottom, 1), g.x0)):
        for group in groups:
            last = group[-1]
            same_line = abs(last.bottom - g.bottom) <= 0.6 * max(last.size,
                                                                 g.size)
            adjacent = g.x0 - last.x1 <= 1.2 * max(last.size, g.size)
            if same_line and adjacent:
                group.append(g)
                break
        else:
            groups.append([g])
    return groups


def _limits(op: Glyph, others: Sequence[Glyph]):
    """Runs sitting above and below a big operator are its limits."""
    slack = LIMIT_SLACK * max(op.x1 - op.x0, op.size)
    lo, hi, eaten = [], [], set()
    for group in _cluster(others):
        gx0 = min(g.x0 for g in group)
        gx1 = max(g.x1 for g in group)
        if gx1 < op.x0 - slack or gx0 > op.x1 + slack:
            continue                      # not centred on the operator
        if all(g.bottom <= op.top + 1 for g in group):
            hi.extend(group)
            eaten.update(id(g) for g in group)
        elif all(g.top >= op.bottom - 1 for g in group):
            lo.extend(group)
            eaten.update(id(g) for g in group)
    return lo, hi, eaten


def _scripts(main: List[Glyph], others: List[Glyph], piece_of: dict,
             baseline: float, base: float):
    """Group raised/dropped glyphs onto the token they follow."""
    grouped: dict = {}
    for g in sorted(others, key=lambda g: g.x0):
        owner = None
        for idx, m in enumerate(main):
            if m.x1 <= g.x0 + 1 and idx in piece_of:
                owner = piece_of[idx]
        if owner is None:
            raise Unreconstructable("script with nothing to attach to")
        kind = "sup" if g.bottom < baseline - SCRIPT_RISE * base * 0.5 else "sub"
        grouped.setdefault((owner, kind), []).append(g)
    return [(owner, kind, group)
            for (owner, kind), group in sorted(grouped.items())]


# --------------------------------------------------------------------------
# token runs
# --------------------------------------------------------------------------

def _function_name(main: List[Glyph], i: int):
    """A run of letters naming an operator (sin, log, lim, ...)."""
    letters = ""
    for g in main[i:]:
        if len(g.text) == 1 and g.text.isascii() and g.text.isalpha():
            letters += g.text
        else:
            break
    for name in FUNCTION_NAMES:          # longest names first in the table
        if letters.startswith(name):
            return name, len(name)
    return None, 0


def _number(main: List[Glyph], i: int):
    """Consecutive digits are one number, not a product of digits."""
    digits = ""
    for g in main[i:]:
        if g.text.isdigit() or (g.text == "." and digits):
            digits += g.text
        else:
            break
    digits = digits.rstrip(".")
    return (digits, len(digits)) if len(digits) > 1 else (None, 0)


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------

def reconstruct(chars: Sequence[dict], bars: Sequence[Bar]) -> Optional[str]:
    """LaTeX for one displayed equation, or None if it isn't certain."""
    glyphs = glyphs_from_chars(chars)
    if not glyphs:
        return None
    try:
        latex = _expression(glyphs, list(bars))
    except Unreconstructable:
        return None
    except RecursionError:
        return None
    latex = " ".join(latex.split())
    return latex or None
