"""Closed host validation for bounded inspection receipts."""

from .model import EvidenceError, FEATURES
from .canonical import canonical


def validate_inspection(value, *, limits, source_hash, source_bytes):
    try:
        if set(value) != {
            "source",
            "page_count",
            "pages",
            "ocr_available",
            "ocr_languages",
            "supported_features",
            "anticipated_gaps",
            "limits",
        }:
            raise ValueError()
        if (
            value["source"] != {"sha256": source_hash, "bytes": source_bytes}
            or type(value["page_count"]) is not int
            or not 1 <= value["page_count"] <= limits.pages
        ):
            raise ValueError()
        if type(value["ocr_available"]) is not bool or value[
            "supported_features"
        ] != list(FEATURES):
            raise ValueError()
        from dataclasses import asdict

        if value["limits"] != asdict(limits):
            raise ValueError()
        languages = value["ocr_languages"]
        import re

        if (
            not isinstance(languages, list)
            or languages != sorted(set(languages))
            or any(
                not isinstance(v, str) or not re.fullmatch("[A-Za-z0-9_]{1,64}", v)
                for v in languages
            )
        ):
            raise ValueError()
        if len(value["pages"]) != value["page_count"]:
            raise ValueError()
        limits.check(
            "observations", sum(page["digital_observations"] for page in value["pages"])
        )
        for n, page in enumerate(value["pages"], 1):
            if set(page) != {
                "number",
                "width_mpt",
                "height_mpt",
                "digital_observations",
                "raster_area_bp",
                "suspect_observations",
                "route",
                "expanded_content_bytes",
            }:
                raise ValueError()
            if page["number"] != n or page["route"] not in ("digital", "ocr"):
                raise ValueError()
            for k, v in page.items():
                if k != "route" and (type(v) is not int or v < 0 or v > 2**53 - 1):
                    raise ValueError()
            if (
                not page["width_mpt"]
                or not page["height_mpt"]
                or page["raster_area_bp"] > 10000
                or page["suspect_observations"] > page["digital_observations"]
            ):
                raise ValueError()
            limits.check("page_content_bytes", page["expanded_content_bytes"])
            limits.check("observations", page["digital_observations"])
        gaps = [
            {"page": p["number"], "code": "OCR_UNAVAILABLE"}
            for p in value["pages"]
            if p["route"] == "ocr" and not value["ocr_available"]
        ]
        if value["anticipated_gaps"] != gaps:
            raise ValueError()
        limits.check("artifact_bytes", len(canonical(value)))
    except EvidenceError:
        raise
    except Exception as exc:
        raise EvidenceError(
            "INVALID_RESULT", "inspect", "Invalid inspection receipt"
        ) from exc
