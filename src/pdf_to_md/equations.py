"""Detection of mathematical content (Tier A: detect and quarantine).

A PDF stores an equation as positioned glyphs, not as an expression, so
there is no faithful plain-text form of it.  Rather than let a garbled
equation flow into a paragraph and read as prose, a display equation is
recognised and emitted as its own labelled block: the glyph run is
preserved verbatim and marked as unreliable, so a reader (or an agent)
can see what it is instead of quietly trusting it.

Two independent signals, both deterministic:

  * **font** -- TeX and journal math is set in dedicated fonts (Computer
    Modern's CMMI/CMSY/CMEX, MathTime, STIX/XITS Math, Symbol, ...).
  * **glyphs** -- characters from the Unicode mathematical blocks, plus
    Greek letters and the common operators.

Reconstructing the expression itself (fractions from rule geometry,
scripts from baseline offsets) is a separate, much larger job; this
module deliberately stops at "this is maths, and here it is verbatim".
"""

from __future__ import annotations

import re
import unicodedata
from typing import List, Optional

# Font names that indicate mathematical typesetting.  PDF font names are
# usually subset-tagged ("ABCDEF+CMMI10"), so this is a search, not a match.
MATH_FONT_RE = re.compile(
    r"(CMMI|CMSY|CMEX|CMBSY|MSAM|MSBM|EUSM|EUFM|EURM|RSFS|"
    r"MathematicalPi|MTMI|MTSY|MTEX|LMMath|"
    r"STIX.{0,12}Math|XITS.{0,12}Math|Cambria\s*Math|Asana.?Math|"
    r"Symbol|TeX-?[A-Za-z]*|Math[A-Za-z]*Italic)",
    re.IGNORECASE,
)

# Individual characters that only appear in mathematics.
_MATH_RANGES = (
    (0x2070, 0x209F),    # super/subscripts
    (0x2190, 0x21FF),    # arrows
    (0x2200, 0x22FF),    # mathematical operators
    (0x2300, 0x23BF),    # misc technical (braces, integrals extensions)
    (0x25A0, 0x25FF),    # geometric shapes used as operators
    (0x27C0, 0x27EF),    # misc mathematical symbols A
    (0x2900, 0x297F),    # supplemental arrows
    (0x2980, 0x29FF),    # misc mathematical symbols B
    (0x2A00, 0x2AFF),    # supplemental mathematical operators
    (0x1D400, 0x1D7FF),  # mathematical alphanumeric symbols
)
_MATH_EXTRA = set("∑∏∫√∂∇∞±×÷≤≥≠≈≡∈∉⊂⊃∪∩⟨⟩ƒ°′″")

# An equation number at the end of the line: (3), (3.1), (A.2), [12]
EQ_TAG_RE = re.compile(r"[\(\[]\s*([A-Za-z]?\.?\s*\d+(?:\.\d+)*[a-z]?)\s*[\)\]]\s*$")

# A run of ordinary letters -- used to tell prose from notation.
PROSE_WORD_RE = re.compile(r"[A-Za-z]{3,}")

MATH_SCORE = 0.30        # fraction of glyphs that must look mathematical
MIN_MATH_CHARS = 4       # below this any line is too short to judge
MAX_PROSE_WORDS = 3      # more ordinary words than this means it's a sentence


def is_math_font(fontname: Optional[str]) -> bool:
    return bool(fontname) and bool(MATH_FONT_RE.search(fontname))


def is_math_char(ch: str) -> bool:
    o = ord(ch)
    if ch in _MATH_EXTRA:
        return True
    if any(lo <= o <= hi for lo, hi in _MATH_RANGES):
        return True
    # Greek letters are the working alphabet of mathematics
    return 0x0370 <= o <= 0x03FF and unicodedata.category(ch).startswith("L")


def math_evidence(words) -> float:
    """Fraction of a line's glyphs that look mathematical (0..1).

    A glyph counts when it is a mathematical character, or when the word
    containing it is set in a mathematical font.  Words carry ``fontname``
    only on the vector path; on the OCR path the character signal alone
    does the work.
    """
    total = math = 0
    for w in words:
        text = (w.get("text") or "").replace(" ", "")
        if not text:
            continue
        font_is_math = is_math_font(w.get("fontname"))
        total += len(text)
        if font_is_math:
            math += len(text)
        else:
            math += sum(1 for ch in text if is_math_char(ch))
    return (math / total) if total else 0.0


def equation_tag(text: str) -> Optional[str]:
    """The equation number at the end of a line, if there is one."""
    m = EQ_TAG_RE.search(text.strip())
    return re.sub(r"\s+", "", m.group(1)) if m else None


# A line that is nothing but an equation number, right-aligned beside the
# equation it belongs to (the usual journal layout).
BARE_TAG_RE = re.compile(r"^[\(\[]\s*([A-Za-z]?\.?\s*\d+(?:\.\d+)*[a-z]?)\s*[\)\]]$")


def bare_tag(text: str) -> Optional[str]:
    """The number of a standalone "(3)" label, else None."""
    m = BARE_TAG_RE.match(text.strip())
    return re.sub(r"\s+", "", m.group(1)) if m else None


def is_equation_line(text: str, evidence: float) -> bool:
    """Whether a whole line should be quarantined as an equation.

    Deliberately conservative: a sentence that merely mentions a Greek
    letter keeps flowing as prose, because its evidence stays low and its
    ordinary words disqualify it.  Failing to detect an equation leaves
    the current (garbled-in-paragraph) behaviour; over-detecting would
    tear a paragraph in half, which is worse.
    """
    stripped = text.strip()
    if not stripped:
        return False
    body = EQ_TAG_RE.sub("", stripped).strip()
    if len(body.replace(" ", "")) < MIN_MATH_CHARS:
        return False
    if len(PROSE_WORD_RE.findall(body)) > MAX_PROSE_WORDS:
        return False
    return evidence >= MATH_SCORE


def render_equation(texts: List[str], tag: Optional[str]) -> str:
    """A verbatim, clearly-labelled block -- never a LaTeX claim.

    The fence is tagged ``equation`` rather than ``math``: renderers treat
    a ``math`` fence as LaTeX and would typeset this glyph soup as if it
    were meaningful.
    """
    label = f"equation {tag}" if tag else "equation"
    note = (f"<!-- {label}: glyphs as laid out in the PDF; "
            f"not a faithful transcription -->")
    body = "\n".join(t for t in texts if t.strip())
    fence = "`" * max(3, 1 + max((len(m.group()) for m in re.finditer(r"`+", body)), default=0))
    return note + "\n" + fence + "equation\n" + body + "\n" + fence


def render_latex(latex: str, tag: Optional[str]) -> str:
    """A reconstructed equation, labelled as reconstructed.

    Unlike the verbatim fence this really is LaTeX, so it is emitted as a
    maths block -- but the comment still says where it came from, because
    a reconstruction is inference from layout, not a transcript the PDF
    ever contained.
    """
    label = f"equation {tag}" if tag else "equation"
    note = (f"<!-- {label}: reconstructed from glyph geometry; "
            f"check against the source -->")
    return note + "\n$$\n" + latex + "\n$$"
