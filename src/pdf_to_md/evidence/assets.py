"""Explicit noncanonical asset envelope and source-bound PNG generation."""

import io
import struct
from dataclasses import dataclass
from .model import EvidenceError
from .render import withheld_pages


@dataclass
class EvidencePackage:
    document: dict
    assets: dict


def asset_scope(document):
    entries = [e for e in document["recipe"]["exports"] if e["kind"] == "png"]
    return entries[0]["options"]["scope"] if entries else "none"


def designated_spans(document):
    """One review region per block, plus explicitly issue-bound source regions."""
    spans = document["spans"]

    def area(sid):
        x0, y0, x1, y1 = spans[sid]["bbox_mpt"]
        return (x1 - x0) * (y1 - y0)

    ids = {max(b["source_span_ids"], key=area) for b in document["blocks"].values()}
    ids.update(
        sid
        for issue in document["issues"].values()
        if issue["scope"] == "span"
        for sid in issue["target_ids"]
    )
    return ids


def expected_assets(document):
    scope = asset_scope(document)
    spans = document["spans"]
    withheld = withheld_pages(document)
    ids = (
        designated_spans(document)
        if scope == "regions"
        else {
            b["payload"]["asset_name"][5:-4]
            for b in document["blocks"].values()
            if b["payload"].get("asset_name") is not None
        }
    )
    return {
        f"crop_{sid}.png": dict(
            span_id=sid,
            exposure="restricted" if spans[sid]["page"] in withheld else "ordinary",
        )
        for sid in sorted(ids)
    }


def render_page_assets(document, page, result, assets, limits):
    scope = asset_scope(document)
    if scope == "none":
        return
    selected = (
        {
            sid
            for sid in designated_spans(document)
            if document["spans"][sid]["page"] == result["number"]
        }
        if scope == "regions"
        else set()
    )
    for b in document["blocks"].values():
        if b["page"] != result["number"]:
            continue
        if b["type"] not in ("figure", "equation", "unresolved_region"):
            continue
        sid = max(
            b["source_span_ids"],
            key=lambda sid: (
                document["spans"][sid]["bbox_mpt"][2]
                - document["spans"][sid]["bbox_mpt"][0]
            )
            * (
                document["spans"][sid]["bbox_mpt"][3]
                - document["spans"][sid]["bbox_mpt"][1]
            ),
        )
        selected.add(sid)
        b["payload"]["asset_name"] = f"crop_{sid}.png"
    if not selected:
        return
    image = page.to_image(resolution=document["recipe"]["ocr"]["dpi"]).original.convert(
        "RGB"
    )
    width, height = result["width_mpt"], result["height_mpt"]
    from math import floor, ceil

    class BoundedBytes(io.BytesIO):
        def write(self, data):
            limits.check(
                "artifact_bytes",
                sum(len(v) for v in assets.values()) + self.tell() + len(data),
            )
            return super().write(data)

    try:
        for sid in sorted(selected):
            span = document["spans"][sid]
            x0, y0, x1, y1 = span["bbox_mpt"]
            box = (
                floor(x0 * image.width / width),
                floor(y0 * image.height / height),
                ceil(x1 * image.width / width),
                ceil(y1 * image.height / height),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                raise EvidenceError(
                    "ASSET_RENDER_FAILED", "extract", "Source region cannot be rendered"
                )
            out = BoundedBytes()
            crop = image.crop(box)
            try:
                crop.save(out, format="PNG", optimize=False, compress_level=9)
            finally:
                crop.close()
            assets[f"crop_{sid}.png"] = out.getvalue()
            out.close()
    finally:
        image.close()


def validate_png(data, limits):
    if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise EvidenceError("INVALID_RESULT", "bundle", "Invalid PNG artifact")
    width, height = struct.unpack(">II", data[16:24])
    if not width or not height:
        raise EvidenceError("INVALID_RESULT", "bundle", "Invalid PNG geometry")
    limits.check("page_pixels", width * height)
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
    except Exception as exc:
        raise EvidenceError("INVALID_RESULT", "bundle", "Invalid PNG artifact") from exc
