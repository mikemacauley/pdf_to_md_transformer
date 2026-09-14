"""Build 'scanned' (image-only) fixture PDFs from the digital fixtures.

Renders each digital fixture to a 200-dpi image, applies a slight
rotation and deterministic noise (seeded RNG) to imitate an aged
photocopy, and saves the result as an image-only PDF -- no text layer.
Dev-only; requires pypdfium2 (via pdfplumber) and numpy.
"""

import os

import numpy as np
import pdfplumber
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "fixtures")

SKEW_DEGREES = 0.6
NOISE_SEED = 42


def _degrade(img: Image.Image, rng: np.random.RandomState) -> Image.Image:
    img = img.convert("L")
    img = img.rotate(SKEW_DEGREES, fillcolor=255, resample=Image.BICUBIC,
                     expand=False)
    arr = np.asarray(img).astype(np.int16)
    # mild gaussian gray noise + sparse pepper specks
    arr = arr + rng.normal(0, 6, arr.shape).astype(np.int16)
    pepper = rng.random_sample(arr.shape) < 0.0008
    arr[pepper] = 40
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def scanify(src: str, dst: str) -> str:
    rng = np.random.RandomState(NOISE_SEED)
    pages = []
    with pdfplumber.open(src) as pdf:
        for page in pdf.pages:
            img = page.to_image(resolution=200).original
            pages.append(_degrade(img, rng))
    pages[0].save(dst, save_all=True, append_images=pages[1:],
                  resolution=200.0)
    return dst


def build(dst):
    """Build the single-page provenance control from the synthetic sample."""
    from make_fixture import build as build_sample
    sample = os.path.join(FIX, "sample.pdf")
    if not os.path.exists(sample):
        build_sample(sample)
    return scanify(sample, dst)


def build_all():
    from make_fixture import build as build_sample
    from make_science_fixture import build as build_science

    sample = os.path.join(FIX, "sample.pdf")
    science = os.path.join(FIX, "science.pdf")
    if not os.path.exists(sample):
        build_sample(sample)
    if not os.path.exists(science):
        build_science(science)

    out = [
        scanify(science, os.path.join(FIX, "scanned_science.pdf")),
        scanify(sample, os.path.join(FIX, "scanned_sample.pdf")),
    ]
    return out


if __name__ == "__main__":
    for p in build_all():
        print(p)
