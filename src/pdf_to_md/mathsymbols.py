"""Glyph -> Unicode -> LaTeX tables for equation reconstruction.

A PDF names glyphs by font-specific code, not by meaning.  Two steps
recover the meaning:

1. **Font encoding.**  Text set in the Adobe ``Symbol`` font arrives from
   the extractor as the raw byte's Latin-1 lookalike (``'¶'`` for 0xB6)
   or as ``(cid:182)`` when there is no lookalike -- both of which give
   back the code, so one table maps the font to real Unicode.  Fonts that
   carry a ToUnicode CMap (most modern LaTeX output) already arrive as
   proper Unicode and skip this step.

2. **LaTeX naming.**  Unicode operators map to their LaTeX commands.

Deliberately incomplete, and that is the point: only mappings that are
certain are listed here.  An unrecognised glyph in a maths font makes the
caller abandon reconstruction and fall back to the verbatim glyph dump,
because a wrong formula is far worse than an obviously raw one.  The
Computer Modern maths encodings (CMMI/CMSY/CMEX) are *not* transcribed
here for exactly that reason -- PDFs using them almost always ship a
ToUnicode CMap, and guessing the rest would invent equations.
"""

from __future__ import annotations

import re
from typing import Optional

CID_RE = re.compile(r"^\(cid:(\d+)\)$")

# --------------------------------------------------------------------------
# Adobe Symbol font encoding (code point -> Unicode)
# --------------------------------------------------------------------------

_GREEK_UPPER = {
    0x41: "Α", 0x42: "Β", 0x43: "Χ", 0x44: "Δ", 0x45: "Ε", 0x46: "Φ",
    0x47: "Γ", 0x48: "Η", 0x49: "Ι", 0x4B: "Κ", 0x4C: "Λ", 0x4D: "Μ",
    0x4E: "Ν", 0x4F: "Ο", 0x50: "Π", 0x51: "Θ", 0x52: "Ρ", 0x53: "Σ",
    0x54: "Τ", 0x55: "Υ", 0x57: "Ω", 0x58: "Ξ", 0x59: "Ψ", 0x5A: "Ζ",
}
_GREEK_LOWER = {
    0x61: "α", 0x62: "β", 0x63: "χ", 0x64: "δ", 0x65: "ε", 0x66: "φ",
    0x67: "γ", 0x68: "η", 0x69: "ι", 0x6A: "ϕ", 0x6B: "κ", 0x6C: "λ",
    0x6D: "μ", 0x6E: "ν", 0x6F: "ο", 0x70: "π", 0x71: "θ", 0x72: "ρ",
    0x73: "σ", 0x74: "τ", 0x75: "υ", 0x77: "ω", 0x78: "ξ", 0x79: "ψ",
    0x7A: "ζ",
}
_SYMBOL_OPERATORS = {
    0x2B: "+", 0x2D: "−", 0x2F: "/", 0x3D: "=", 0x3C: "<", 0x3E: ">",
    0x28: "(", 0x29: ")", 0x5B: "[", 0x5D: "]", 0x7B: "{", 0x7D: "}",
    0x2C: ",", 0x2E: ".", 0x21: "!", 0x7C: "|", 0x3A: ":", 0x3B: ";",
    0xA3: "≤", 0xA5: "∞", 0xB1: "±", 0xB3: "≥", 0xB4: "×", 0xB6: "∂",
    0xB7: "•", 0xB8: "÷", 0xB9: "≠", 0xBA: "≡", 0xBB: "≈",
    0xAB: "↔", 0xAC: "←", 0xAD: "↑", 0xAE: "→", 0xAF: "↓",
    0xC7: "∩", 0xC8: "∪", 0xCE: "∈", 0xCF: "∉",
    0xD1: "∇", 0xD6: "√", 0xE5: "∑", 0xF2: "∫",
    0xB0: "°", 0xD8: "¬",
}
SYMBOL_ENCODING = {}
SYMBOL_ENCODING.update(_GREEK_UPPER)
SYMBOL_ENCODING.update(_GREEK_LOWER)
SYMBOL_ENCODING.update(_SYMBOL_OPERATORS)
# digits and the ASCII characters Symbol leaves alone
SYMBOL_ENCODING.update({c: chr(c) for c in range(0x30, 0x3A)})

SYMBOL_FONT_RE = re.compile(r"symbol", re.IGNORECASE)

# Fonts whose glyphs are pictures, not the letters their codes look like:
# ZapfDingbats "n" is a filled square, not an n.  Text extracted from one
# of these is meaningless as maths and must never be read literally.
PICTORIAL_FONT_RE = re.compile(r"dingbat|wingding|webding|marlett",
                               re.IGNORECASE)


# --------------------------------------------------------------------------
# Unicode -> LaTeX
# --------------------------------------------------------------------------

UNICODE_TO_LATEX = {
    # relations
    "≤": r"\leq", "≥": r"\geq", "≠": r"\neq", "≡": r"\equiv",
    "≈": r"\approx", "∼": r"\sim", "∝": r"\propto",
    # operators
    "±": r"\pm", "∓": r"\mp", "×": r"\times", "÷": r"\div",
    "⋅": r"\cdot", "·": r"\cdot", "•": r"\bullet", "∘": r"\circ", "−": "-",
    # big operators
    "∑": r"\sum", "∏": r"\prod", "∫": r"\int", "∮": r"\oint",
    "⋃": r"\bigcup", "⋂": r"\bigcap",
    # calculus / logic / sets
    "∂": r"\partial", "∇": r"\nabla", "∞": r"\infty", "√": r"\sqrt",
    "∈": r"\in", "∉": r"\notin", "⊂": r"\subset", "⊃": r"\supset",
    "∪": r"\cup", "∩": r"\cap", "∅": r"\emptyset",
    "∀": r"\forall", "∃": r"\exists", "¬": r"\neg",
    # arrows
    "→": r"\to", "←": r"\leftarrow", "↔": r"\leftrightarrow",
    "↑": r"\uparrow", "↓": r"\downarrow", "⇒": r"\Rightarrow",
    "⇐": r"\Leftarrow", "⇔": r"\Leftrightarrow",
    # misc
    "°": r"^{\circ}", "′": r"'", "″": r"''", "…": r"\dots",
    "ℓ": r"\ell", "ℏ": r"\hbar",
}

_GREEK_LATEX = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "ε": "epsilon", "ζ": "zeta", "η": "eta", "θ": "theta",
    "ι": "iota", "κ": "kappa", "λ": "lambda", "μ": "mu", "ν": "nu",
    "ξ": "xi", "ο": "o", "π": "pi", "ρ": "rho", "σ": "sigma",
    "τ": "tau", "υ": "upsilon", "φ": "phi", "ϕ": "varphi",
    "χ": "chi", "ψ": "psi", "ω": "omega", "ϖ": "varpi", "ς": "varsigma",
    "Γ": "Gamma", "Δ": "Delta", "Θ": "Theta", "Λ": "Lambda", "Ξ": "Xi",
    "Π": "Pi", "Σ": "Sigma", "Υ": "Upsilon", "Φ": "Phi", "Ψ": "Psi",
    "Ω": "Omega",
}
for _ch, _name in _GREEK_LATEX.items():
    UNICODE_TO_LATEX.setdefault(_ch, "\\" + _name)

# Greek capitals that are just Latin letters in disguise render as
# themselves; LaTeX has no \Alpha.
UNICODE_TO_LATEX.update({
    "ο": "o",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I",
    "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T",
    "Χ": "X",
})

# Characters that mean something else in LaTeX source.
LATEX_ESCAPES = {
    "%": r"\%", "&": r"\&", "#": r"\#", "$": r"\$",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\sim",
}

# Multi-letter names that are operators, not a product of variables.
FUNCTION_NAMES = (
    "arccos", "arcsin", "arctan", "cosh", "sinh", "tanh", "coth",
    "det", "dim", "exp", "gcd", "hom", "inf", "ker", "lim", "log",
    "max", "min", "sup", "arg", "deg", "cos", "sin", "tan", "sec",
    "csc", "cot", "ln", "lg", "Pr",
)

# Operators that take limits above and below rather than as scripts.
BIG_OPERATORS = frozenset("∑∏∫∮⋃⋂")

# Plain ASCII that needs no translation at all.
_PLAIN = set("abcdefghijklmnopqrstuvwxyz"
             "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
             "0123456789+-=<>()[]|,.!:;/'")


def char_code(text: str) -> Optional[int]:
    """The font code behind an extracted glyph, or None if it isn't one."""
    m = CID_RE.match(text)
    if m:
        return int(m.group(1))
    return ord(text) if len(text) == 1 else None


def is_symbol_font(fontname: Optional[str]) -> bool:
    return bool(fontname) and bool(SYMBOL_FONT_RE.search(fontname))


def is_pictorial_font(fontname: Optional[str]) -> bool:
    return bool(fontname) and bool(PICTORIAL_FONT_RE.search(fontname))


def to_unicode(text: str, fontname: Optional[str]) -> Optional[str]:
    """Real Unicode for one extracted glyph, or None if it can't be known.

    Returning None is a legitimate, common answer -- it makes the caller
    abandon reconstruction rather than emit a guess.
    """
    if not text:
        return None
    if is_pictorial_font(fontname):
        return None            # the code is a picture, not the letter it looks
    if is_symbol_font(fontname):
        code = char_code(text)
        return SYMBOL_ENCODING.get(code) if code is not None else None
    if CID_RE.match(text):
        return None            # unmapped glyph in some other font
    return text


def to_latex(ch: str) -> Optional[str]:
    """LaTeX for one Unicode character, or None if it isn't known."""
    if ch in LATEX_ESCAPES:
        return LATEX_ESCAPES[ch]
    if ch in UNICODE_TO_LATEX:
        return UNICODE_TO_LATEX[ch]
    if ch in _PLAIN or ch == " ":
        return ch
    return None
