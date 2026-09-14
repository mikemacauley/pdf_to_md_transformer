"""OCR support for scanned (image-only) PDF pages.

Uses the Tesseract engine (Apache-2.0) through pytesseract.  All
post-processing is deterministic; Tesseract itself is deterministic for
a fixed version + traineddata, so output is reproducible per install.

Provides:
  * find_tesseract()      -- locate the tesseract binary (bundled, PATH,
                             or well-known install locations)
  * tesseract_version()   -- version string, for the provenance marker
  * page_needs_ocr(page)  -- True when the page has no usable text layer
  * ocr_page(page, opts)  -- render, deskew, OCR -> word boxes in PDF
                             points, plus detected horizontal and vertical
                             rule lines from pixel analysis

OCR is the one part of the pipeline that can be wrong in ways geometry
cannot detect, so every OCR result carries its provenance (engine
version, page-segmentation mode, dpi, language) and every numeric token
that is doubtful is flagged: low confidence, or disagreement with a
second read of the same crop restricted to digits.  Verifying a page then
means checking a handful of marked numbers rather than the whole page.
"""

from __future__ import annotations

import os
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image

DPI = 300
MIN_WORD_CONF = 30.0        # tesseract confidence floor; below is noise
NUMERIC_CONF = 80.0         # numeric tokens below this are flagged as doubtful
MAX_RECHECKS = 80           # digit re-reads per page (each is a subprocess)
RECHECK_PAD_PX = 4          # crop padding for the digit re-read
BINARIZE_THRESHOLD = 160    # gray level; darker = ink
LINE_DARK_FRAC = 0.62       # windowed dark fraction that counts as "line"
LINE_WINDOW_PX = 31         # smoothing window along the line direction
H_LINE_MIN_FRAC = 0.10      # min h-line length as fraction of page width
V_LINE_MIN_PX = 110         # min v-line length in pixels (@300dpi ~ 26pt)
MAX_LINE_THICKNESS_PX = 14  # thicker dark bands are images, not rules
MAX_RENDER_PIXELS = 40_000_000


def check_render_size(width, height, dpi):
    """Reject excessive raster allocations before invoking the PDF renderer."""
    if (not math.isfinite(width) or not math.isfinite(height)
            or width <= 0 or height <= 0
            or math.ceil(width * dpi / 72) * math.ceil(height * dpi / 72)
            > MAX_RENDER_PIXELS):
        raise ValueError("rendered image exceeds the 40 million pixel limit or has invalid dimensions")

_tesseract_cmd: Optional[str] = None
_checked = False
_version: Optional[str] = None


@dataclass(frozen=True)
class OcrOptions:
    """Knobs that change OCR output -- recorded in the page marker."""
    lang: str = "eng"
    psm: int = 3
    dpi: int = DPI
    verify_numbers: bool = True

    def __post_init__(self):
        # Language names are identifiers, never extra Tesseract arguments.
        if not isinstance(self.lang, str) or not re.fullmatch(
                r"[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*(?:\+[A-Za-z0-9_]+(?:/[A-Za-z0-9_]+)*)*",
                self.lang):
            raise ValueError("OCR language must be a language identifier (e.g. eng or fra+eng)")
        if type(self.psm) is not int or self.psm not in (1, *range(3, 14)):
            raise ValueError("OCR psm must be 1 or an integer from 3 to 13")
        if type(self.dpi) is not int or not 72 <= self.dpi <= 1200:
            raise ValueError("OCR dpi must be an integer from 72 to 1200")
        if type(self.verify_numbers) is not bool:
            raise ValueError("verify_numbers must be a boolean")


def find_tesseract() -> Optional[str]:
    """Locate tesseract: env var, bundled copy, PATH, standard installs."""
    global _tesseract_cmd, _checked
    if _checked:
        return _tesseract_cmd
    _checked = True

    exe = "tesseract.exe" if os.name == "nt" else "tesseract"
    candidates = []
    env = os.environ.get("PDF2MD_TESSERACT")
    if env:
        candidates.append(env)
    # bundled next to the frozen executable (Windows installer layout)
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable),
                                       "Tesseract-OCR", exe))
    which = shutil.which("tesseract")
    if which:
        candidates.append(which)
    candidates += [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/opt/homebrew/bin/tesseract",
        "/usr/local/bin/tesseract",
        "/usr/bin/tesseract",
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            _tesseract_cmd = c
            tessdata = os.path.join(os.path.dirname(c), "tessdata")
            if os.path.isdir(tessdata) and "TESSDATA_PREFIX" not in os.environ:
                os.environ["TESSDATA_PREFIX"] = tessdata
            break
    return _tesseract_cmd


def ocr_available() -> bool:
    return find_tesseract() is not None


def tesseract_version() -> Optional[str]:
    """Version of the installed engine, e.g. "5.3.4" (None if absent).

    Output is reproducible only for a fixed engine version, so the version
    belongs in the output next to the text it produced.
    """
    global _version
    if _version is not None:
        return _version or None
    cmd = find_tesseract()
    if cmd is None:
        return None
    try:
        out = subprocess.run([cmd, "--version"], capture_output=True,
                             text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        _version = ""
        return None
    m = re.search(r"tesseract\s+v?([0-9][0-9A-Za-z.\-]*)", out)
    _version = m.group(1) if m else ""
    return _version or None


def page_needs_ocr(page) -> bool:
    """True when the page has essentially no extractable text layer."""
    usable = [ch for ch in page.chars if (ch.get("text") or "").strip()]
    return not usable or (len(usable) < 5 and bool(page.images))


# --------------------------------------------------------------------------
# image processing (deterministic, numpy only)
# --------------------------------------------------------------------------

def _deskew_with_angle(img: Image.Image):
    """Return deskewed pixels and the applied angle for inverse source mapping."""
    small = img.resize((max(1, img.width // 4), max(1, img.height // 4)))
    best_angle, best_score = 0.0, -1.0
    for q in range(-12, 13):                       # -3.0 .. +3.0 in 0.25 steps
        angle = q / 4.0
        rot = small.rotate(angle, fillcolor=255) if angle else small
        arr = np.asarray(rot) < BINARIZE_THRESHOLD
        score = float(np.var(arr.sum(axis=1)))
        if score > best_score + 1e-6:
            best_angle, best_score = angle, score
    if abs(best_angle) >= 0.3:
        return img.rotate(best_angle, fillcolor=255, resample=Image.BICUBIC), best_angle
    return img, 0.0


def _deskew(img: Image.Image) -> Image.Image:
    return _deskew_with_angle(img)[0]


def _windowed_mean(mat: np.ndarray, win: int, axis: int) -> np.ndarray:
    """Centered moving average along an axis (same shape, edge-padded)."""
    kernel_shape = [1, 1]
    kernel_shape[axis] = win
    pad = [(0, 0), (0, 0)]
    pad[axis] = (win // 2, win - win // 2 - 1)
    padded = np.pad(mat.astype(np.float32), pad, mode="edge")
    cs = np.cumsum(padded, axis=axis)
    if axis == 0:
        out = (cs[win - 1:, :] - np.vstack([np.zeros((1, cs.shape[1]), np.float32),
                                            cs[:-win, :]]))
    else:
        out = (cs[:, win - 1:] - np.hstack([np.zeros((cs.shape[0], 1), np.float32),
                                            cs[:, :-win]]))
    return out / win


def _detect_rules_px(binary: np.ndarray) -> Tuple[List[dict], List[dict]]:
    """Find horizontal and vertical ruled lines in a binarized page image.

    Returns (h_lines, v_lines) in pixel coordinates:
      h_lines: {x0, x1, top}
      v_lines: {top, bottom, x}
    """
    H, W = binary.shape

    def scan(mat, min_len):
        """Rows of `mat` whose smoothed dark runs exceed min_len."""
        smooth = _windowed_mean(mat, LINE_WINDOW_PX, axis=1)
        mask = smooth >= LINE_DARK_FRAC
        hits = []
        for y in range(mat.shape[0]):
            row = mask[y]
            if not row.any():
                continue
            xs = np.flatnonzero(row)
            # longest consecutive run
            splits = np.flatnonzero(np.diff(xs) > 1)
            runs = np.split(xs, splits + 1)
            run = max(runs, key=len)
            if len(run) >= min_len:
                hits.append((y, int(run[0]), int(run[-1])))
        # group consecutive hit-rows into physical lines
        lines = []
        cur = []
        for h in hits:
            if cur and h[0] - cur[-1][0] <= 2:
                cur.append(h)
            else:
                if cur:
                    lines.append(cur)
                cur = [h]
        if cur:
            lines.append(cur)
        out = []
        for grp in lines:
            if len(grp) > MAX_LINE_THICKNESS_PX:
                continue  # image block, not a rule
            ys = [g[0] for g in grp]
            out.append({
                "pos": sum(ys) / len(ys),
                "lo": min(g[1] for g in grp),
                "hi": max(g[2] for g in grp),
            })
        return out

    h_raw = scan(binary, max(int(H_LINE_MIN_FRAC * W), 60))
    v_raw = scan(binary.T, V_LINE_MIN_PX)

    h_lines = [{"x0": float(l["lo"]), "x1": float(l["hi"]), "top": float(l["pos"])}
               for l in h_raw]
    v_lines = [{"top": float(l["lo"]), "bottom": float(l["hi"]), "x": float(l["pos"])}
               for l in v_raw]
    return h_lines, v_lines


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# numeric verification
# --------------------------------------------------------------------------

# A token worth double-checking: contains a digit and is otherwise made of
# characters that appear in numbers (so "1,024.50%" qualifies, "Fig.3b" and
# ordinary words do not).
NUMERIC_RE = re.compile(r"^[(\[]?[+\u2212-]?[$\u00a3\u20ac]?\d[\d.,:/\u00d7x^\u2013\u2014-]*"
                        r"\s?[%\u00b0]?[)\]]?[.,;:]?$")

# Characters tesseract is allowed to return when re-reading a number.
DIGIT_WHITELIST = "0123456789.,%-+()/:$"


def is_numeric_token(text: str) -> bool:
    """True for tokens whose value would be corrupted by a misread glyph."""
    return bool(text) and any(c.isdigit() for c in text) \
        and bool(NUMERIC_RE.match(text))


def normalise_number(text: str) -> str:
    """Strip punctuation that the two reads may legitimately disagree on."""
    text = text.strip().rstrip(";:").replace("−", "-")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    # Locale separators may swap, but losing a separator, sign, ratio or
    # percent marker changes the value and must trigger a warning.
    return re.sub(r"[^0-9.,+%/:-]", "", text).replace(",", ".")


def _reread_digits(img, word, k: float, options: "OcrOptions") -> Optional[str]:
    """Re-OCR one word's crop with a digit whitelist; None if unavailable."""
    import pytesseract
    left = max(0, int(word["x0"] / k) - RECHECK_PAD_PX)
    top = max(0, int(word["top"] / k) - RECHECK_PAD_PX)
    right = min(img.width, int(word["x1"] / k) + RECHECK_PAD_PX)
    bottom = min(img.height, int(word["bottom"] / k) + RECHECK_PAD_PX)
    if right - left < 2 or bottom - top < 2:
        return None
    config = ("--psm 8 "
              f"-c tessedit_char_whitelist={DIGIT_WHITELIST}")
    try:
        out = pytesseract.image_to_string(img.crop((left, top, right, bottom)),
                                          config=config, lang=options.lang,
                                          timeout=10)
    except Exception:
        return None
    return out.strip()


def _flag_numbers(img, words, k: float, options: "OcrOptions") -> None:
    """Annotate doubtful numeric words in place with a 'suspect' reason.

    Two independent signals: the engine's own confidence, and a second
    read of the same pixels restricted to digits.  Either one firing is
    enough to mark the token -- a flagged number is cheap, a silently
    wrong one is not.
    """
    numeric = [w for w in words if is_numeric_token(w["text"])]
    rechecked = 0
    for w in numeric:
        budget_left = rechecked < MAX_RECHECKS
        if options.verify_numbers and budget_left:
            rechecked += 1
            again = _reread_digits(img, w, k, options)
            if again is not None and normalise_number(again) \
                    and normalise_number(again) != normalise_number(w["text"]):
                w["suspect"] = f"reread as {again!r}"
                continue
        if w.get("conf", 100.0) < NUMERIC_CONF:
            reason = f"confidence {w['conf']:.0f}"
            if options.verify_numbers and not budget_left:
                # past the per-page budget: confidence is the only signal
                reason += " (not re-read)"
            w["suspect"] = reason



def _grid_recognition_image(img, dpi):
    """Mask detected grid rules in a copy; retain raw pixels and rule geometry."""
    binary = np.asarray(img) < BINARIZE_THRESHOLD
    h_px, v_px = _detect_rules_px(binary)
    # Retain the original raster for numeric rereads and grid geometry. Thin
    # grid rules otherwise cause Tesseract to discard entire rows as graphics.
    recognition_image = img
    if len(h_px) >= 3 and len(v_px) >= 2:
        from PIL import ImageDraw
        recognition_image = img.copy()
        draw = ImageDraw.Draw(recognition_image)
        for rule in h_px:
            draw.line((rule["x0"], rule["top"], rule["x1"], rule["top"]),
                      fill=255, width=max(1, round(7 * dpi / 300)))
        for rule in v_px:
            draw.line((rule["x"], rule["top"], rule["x"], rule["bottom"]),
                      fill=255, width=max(1, round(7 * dpi / 300)))

    return recognition_image, h_px, v_px

def ocr_page(page, options: Optional[OcrOptions] = None
             ) -> Tuple[List[dict], List[dict], List[dict]]:
    """OCR one pdfplumber page.

    Returns (words, h_rules, v_rules) with all coordinates in PDF points
    (same space the vector pipeline uses).  Words carry ``conf``, and
    doubtful numeric words additionally carry ``suspect`` -- a short
    reason the engine caller can surface.  Raises RuntimeError when no
    tesseract binary can be found.
    """
    options = options or OcrOptions()
    cmd = find_tesseract()
    if cmd is None:
        raise RuntimeError(
            "This page is a scanned image and requires the Tesseract OCR "
            "engine, which was not found. Install it (Windows: bundled "
            "with the installer; macOS: brew install tesseract; Linux: "
            "apt install tesseract-ocr) or set PDF2MD_TESSERACT."
        )
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = cmd

    check_render_size(float(page.width), float(page.height), options.dpi)
    rendered = page.to_image(resolution=options.dpi)
    img = rendered.original.convert("L")
    img = _deskew(img)
    k = 72.0 / options.dpi  # px -> pt

    recognition_image, h_px, v_px = _grid_recognition_image(img, options.dpi)

    data = pytesseract.image_to_data(
        recognition_image, output_type=pytesseract.Output.DICT,
        config=f"--psm {options.psm}", lang=options.lang, timeout=120,
    )
    words = []
    for i in range(len(data["text"])):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1.0
        if not math.isfinite(conf):
            conf = -1.0
        if conf < MIN_WORD_CONF and not is_numeric_token(text):
            continue
        x, y = data["left"][i], data["top"][i]
        w, h = data["width"][i], data["height"][i]
        if w <= 0 or h <= 0:
            continue
        words.append({
            "text": text,
            "x0": x * k, "x1": (x + w) * k,
            "top": y * k, "bottom": (y + h) * k,
            "size": h * k,
            "conf": conf,
        })

    _flag_numbers(img, words, k, options)

    h_rules = [{"x0": l["x0"] * k, "x1": l["x1"] * k, "top": l["top"] * k}
               for l in h_px]
    v_rules = [{"top": l["top"] * k, "bottom": l["bottom"] * k, "x": l["x"] * k}
               for l in v_px]
    # Pixel coordinates start at the rendered crop, which need not start at
    # PDF (0, 0). Use its actual dimensions to account for pixel rounding.
    left, top, right, bottom = rendered.bbox
    sx = (right - left) / img.width / k
    sy = (bottom - top) / img.height / k
    for word in words:
        word["x0"] = left + word["x0"] * sx
        word["x1"] = left + word["x1"] * sx
        word["top"] = top + word["top"] * sy
        word["bottom"] = top + word["bottom"] * sy
        word["size"] *= sy
    for rule in h_rules:
        rule["x0"] = left + rule["x0"] * sx
        rule["x1"] = left + rule["x1"] * sx
        rule["top"] = top + rule["top"] * sy
    for rule in v_rules:
        rule["x"] = left + rule["x"] * sx
        rule["top"] = top + rule["top"] * sy
        rule["bottom"] = top + rule["bottom"] * sy
    return words, h_rules, v_rules
