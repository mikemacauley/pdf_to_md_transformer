"""Affine OCR preprocessing with an explicit inverse to source coordinates."""

import math
from fractions import Fraction
from ..ocr import _deskew_with_angle, _grid_recognition_image
from .coordinates import matrix, project, rectangle


def prepare(image, result, dpi):
    original = image
    image, angle = _deskew_with_angle(image)
    sx = Fraction(result["width_mpt"], image.width)
    sy = Fraction(result["height_mpt"], image.height)
    c = Fraction(str(round(math.cos(math.radians(angle)), 12)))
    s = Fraction(str(round(math.sin(math.radians(angle)), 12)))
    cx, cy = Fraction(image.width, 2), Fraction(image.height, 2)
    # Bound each composed rational, including translation, to the wire safe
    # integer range; at this denominator pixel projection error is sub-mpt.
    inverse = matrix(
        [
            Fraction(value).limit_denominator(1_000_000)
            for value in [
                sx * c,
                -sx * s,
                sx * (cx - c * cx + s * cy),
                sy * s,
                sy * c,
                sy * (cy - s * cx - c * cy),
                0,
                0,
                1,
            ]
        ]
    )
    fid = f'p{result["number"]:04d}_render'
    if angle:
        fid = f'p{result["number"]:04d}_deskew'
        result["frames"].append(
            dict(
                id=fid,
                kind="deskew_pixels",
                width=image.width,
                height=image.height,
                to_page_mpt=inverse,
            )
        )
    recognition, h, v = _grid_recognition_image(image, dpi)
    if original is not image:
        original.close()

    def mapped(box):
        _, b = project(rectangle(box), inverse)
        return [
            max(0, min(result["width_mpt"], b[0])),
            max(0, min(result["height_mpt"], b[1])),
            max(0, min(result["width_mpt"], b[2])),
            max(0, min(result["height_mpt"], b[3])),
        ]

    # Rule regions in source page coordinates conservatively enclose the inverse
    # skewed line. OCR tables are candidates, not claims of precise grid topology.
    hr = []
    vr = []
    for r in h:
        b = mapped([r["x0"], r["top"], r["x1"], r["top"]])
        hr.append(dict(x0=b[0], x1=b[2], top=(b[1] + b[3]) // 2))
    for r in v:
        b = mapped([r["x"], r["top"], r["x"], r["bottom"]])
        vr.append(dict(x=(b[0] + b[2]) // 2, top=b[1], bottom=b[3]))
    return image, recognition, fid, mapped, (hr, vr)


class CellInk:
    """Lazy source-raster check: an empty OCR cell with ink is not a blank."""

    def __init__(self, page, result, dpi):
        self.page = page
        self.result = result
        self.dpi = dpi
        self.image = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.image is not None:
            self.image.close()

    def __call__(self, box):
        if self.image is None:
            self.image = self.page.to_image(resolution=self.dpi).original.convert("L")
        x0, y0, x1, y1 = box
        w, h = self.result["width_mpt"], self.result["height_mpt"]
        # Exclude only the known grid-border band; a too-small interior remains
        # unreadable rather than being certified empty by an empty crop.
        pad = max(1, round(self.dpi / 72))
        cropbox = (
            math.ceil(x0 * self.image.width / w) + pad,
            math.ceil(y0 * self.image.height / h) + pad,
            math.floor(x1 * self.image.width / w) - pad,
            math.floor(y1 * self.image.height / h) - pad,
        )
        if cropbox[2] <= cropbox[0] or cropbox[3] <= cropbox[1]:
            return True
        crop = self.image.crop(cropbox)
        try:
            return crop.getextrema()[0] < 160
        finally:
            crop.close()
