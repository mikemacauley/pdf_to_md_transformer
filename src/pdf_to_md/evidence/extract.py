"""Bounded local PDF evidence SDK. Hostile-input service use requires supervisor."""

import copy
import io
import math
import re
from contextlib import contextmanager
from dataclasses import asdict

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError
from .canonical import canonical, digest, identity
from .coordinates import matrix, millipoints, rectangle
from .model import EvidenceError, FEATURES, Limits, Profile
from .structure import build_page, finalize_document, merge_paragraphs, allocate
from .observations import raster_union_area, visibility_conflict
from .quality import COUNTERS, summarize
from .runtime import engine_digest, inventory
from .validate import validate_document
from ..geometry import words_to_line_groups
from ..ocr import find_tesseract, is_numeric_token, normalise_number, page_needs_ocr


@contextmanager
def _open(source, limits):
    # Read in bounded chunks; do not close or assume seekability of caller stream.
    chunks = []
    size = 0
    while True:
        chunk = source.read(min(1024**2, limits.input_bytes - size + 1))
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise EvidenceError(
                "INVALID_INPUT", "receive", "A binary stream is required"
            )
        size += len(chunk)
        limits.check("input_bytes", size)
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data.startswith(b"%PDF-"):
        raise EvidenceError("UNSUPPORTED_FORMAT", "inspect", "Input is not a PDF")
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if pdf.doc.encryption:
                raise EvidenceError(
                    "ENCRYPTED_INPUT", "inspect", "Encrypted PDFs are unsupported"
                )
            limits.check("pages", len(pdf.pages))
            if not pdf.pages:
                raise EvidenceError("INVALID_INPUT", "inspect", "PDF has no pages")
            yield data, pdf
    except EvidenceError:
        raise
    except PDFEncryptionError:
        raise EvidenceError(
            "ENCRYPTED_INPUT", "inspect", "Encrypted PDFs are unsupported"
        ) from None
    except Exception as exc:
        raise EvidenceError(
            "INVALID_INPUT", "extract", "PDF processing failed"
        ) from exc


def _page(page, limits, profile):
    # pdfplumber supplies rotated top-left CropBox coordinates.
    box = page.cropbox or page.bbox
    page = page.crop(box) if tuple(box) != tuple(page.bbox) else page
    w, h = float(page.width), float(page.height)
    if not all(math.isfinite(v) and v > 0 for v in (w, h)):
        raise EvidenceError("INVALID_INPUT", "inspect", "Invalid page geometry")
    limits.check(
        "page_pixels", math.ceil(w * profile.dpi / 72) * math.ceil(h * profile.dpi / 72)
    )
    expanded = 0
    for content in page.page_obj.contents:
        expanded += len(content.get_data())
        limits.check("page_content_bytes", expanded)
    chars = page.chars
    nonwhite = [c for c in chars if c.get("text", "").strip()]
    suspect = sum(
        "\ufffd" in c.get("text", "") or "(cid:" in c.get("text", "") for c in nonwhite
    )
    area = raster_union_area(page.images, page.bbox)
    needs = (
        page_needs_ocr(page) or suspect * 100 > 2 * len(nonwhite) or area * 5 >= w * h
    )
    route = profile.mode == "ocr_all" or profile.mode == "auto" and needs
    return page, dict(
        width_mpt=millipoints(w),
        height_mpt=millipoints(h),
        digital_observations=len(chars),
        raster_area_bp=round(area / (w * h) * 10000),
        suspect_observations=suspect,
        route="ocr" if route else "digital",
        expanded_content_bytes=expanded,
    )


def inspect_document(source, *, limits=Limits()):
    runtime = inventory()
    with _open(source, limits) as (data, pdf):
        pages = []
        observed = 0
        for n, original in enumerate(pdf.pages, 1):
            page, measurements = _page(original, limits, Profile())
            observed += measurements["digital_observations"]
            limits.check("observations", observed)
            pages.append(dict(number=n, **measurements))
            page.close()
            original.close()
        return dict(
            source=dict(sha256=digest(data), bytes=len(data)),
            page_count=len(pages),
            pages=pages,
            ocr_available=runtime["ocr_binary_sha256"] is not None,
            ocr_languages=[t["language"] for t in runtime["traineddata"]],
            supported_features=list(FEATURES),
            anticipated_gaps=[
                dict(page=p["number"], code="OCR_UNAVAILABLE")
                for p in pages
                if p["route"] == "ocr" and runtime["ocr_binary_sha256"] is None
            ],
            limits=asdict(limits),
        )


def extract_document(source, *, profile=Profile(), limits=Limits(), progress=None):
    if profile.asset_mode != "none":
        raise EvidenceError(
            "INVALID_OPTIONS", "options", "Use extract_package for PNG exports"
        )
    return _extract_document(
        source, profile=profile, limits=limits, progress=progress, assets={}
    )


def extract_package(
    source, *, profile=Profile(asset_mode="figures"), limits=Limits(), progress=None
):
    from .assets import EvidencePackage

    assets = {}
    document = _extract_document(
        source, profile=profile, limits=limits, progress=progress, assets=assets
    )
    return EvidencePackage(document, assets)


def _extract_document(source, *, profile, limits, progress, assets):
    runtime = inventory()
    with _open(source, limits) as (data, pdf):
        requested = list(profile.requested_pages) or list(range(1, len(pdf.pages) + 1))
        if max(requested) > len(pdf.pages):
            raise EvidenceError(
                "INVALID_OPTIONS", "inspect", "Requested page does not exist"
            )
        recipe = dict(
            schema_version="1.0.0",
            engine_build_sha256=engine_digest(),
            profile_id=profile.profile_id,
            routing_policy_id="auto-1",
            normalization_policy_id="conservative-1",
            requested_pages=requested,
            required_features=sorted(profile.required_features),
            ocr={
                k: getattr(profile, k)
                for k in (
                    "mode",
                    "dpi",
                    "psm",
                    "verify_numbers",
                    "max_rereads_per_page",
                    "max_rereads_per_document",
                )
            },
            rules={
                k: dict(version="1", sha256=digest(k.encode()))
                for k in (
                    "issue_catalog",
                    "routing",
                    "normalization",
                    "renderer",
                    "visibility",
                    "blank_assessment",
                    "structure_1",
                    "table_cells_1",
                    "paragraph_join_1",
                    "page_spool_1",
                    "png_export_1",
                    "ocr_affine_1",
                    "grid_rule_mask_1",
                    "cell_ink_1",
                )
            },
            runtime=runtime,
            count_limits=asdict(limits),
            exports=[
                dict(kind=k, required=True, options={})
                for k in ("document", "markdown", "issues")
            ],
        )
        if profile.asset_mode != "none":
            recipe["exports"].append(
                dict(kind="png", required=True, options=dict(scope=profile.asset_mode))
            )
        recipe["ocr"]["languages"] = list(profile.languages)
        rh = digest(canonical(recipe))
        eid = identity(digest(data), rh)
        d = dict(
            schema_version="1.0.0",
            source=dict(
                sha256=digest(data), mime_type="application/pdf", bytes=len(data)
            ),
            extraction_id=eid,
            recipe_hash=rh,
            recipe=recipe,
            scope=dict(
                total_pages=len(pdf.pages),
                requested_pages=requested,
                required_features=recipe["required_features"],
            ),
            pages=[],
            observations={},
            blocks={},
            spans={},
            transforms={},
            issues={},
            quality={},
            extensions={},
        )
        counters = dict.fromkeys(COUNTERS, 0)
        text_count = 0
        metadata = {}
        from .spool import PageSpool

        offsets = {}
        with PageSpool(limits, len(data)) as spool:
            offsets = spool.offsets
            for n in requested:
                metadata = {}
                original = pdf.pages[n - 1]
                page, m = _page(original, limits, profile)
                width, height = m["width_mpt"], m["height_mpt"]
                left, top, _, _ = page.bbox
                fid = f"p{n:04d}_page"
                result = dict(
                    number=n,
                    printed_label=None,
                    width_mpt=width,
                    height_mpt=height,
                    coverage="complete",
                    method="none",
                    blank="not_blank",
                    block_order=[],
                    issue_ids=[],
                    feature_status={
                        f: (
                            "not_present"
                            if f in profile.required_features
                            else "not_requested"
                        )
                        for f in FEATURES
                    },
                    frames=[
                        dict(
                            id=fid,
                            kind="page_mpt",
                            width=width,
                            height=height,
                            to_page_mpt=matrix([1, 0, 0, 0, 1, 0, 0, 0, 1]),
                        )
                    ],
                )
                d["pages"].append(result)

                def issue(
                    code,
                    severity="warning",
                    rule="evidence_1",
                    measurements=None,
                    exposure="ordinary",
                ):
                    limits.check(
                        "issues", offsets.get("issues", 0) + len(d["issues"]) + 1
                    )
                    iid = allocate(d, "issues", offsets)
                    d["issues"][iid] = dict(
                        code=code,
                        severity=severity,
                        scope="page",
                        target_ids=[str(n)],
                        page=n,
                        details=dict(rule_id=rule, measurements=measurements or {}),
                        suggested_action="review_source",
                        exposure=exposure,
                    )
                    result["issue_ids"].append(iid)
                    return iid

                digital = []
                for sequence, ch in enumerate(page.chars):
                    box = [
                        max(0, min(width, millipoints(ch["x0"] - left))),
                        max(0, min(height, millipoints(ch["top"] - top))),
                        max(0, min(width, millipoints(ch["x1"] - left))),
                        max(0, min(height, millipoints(ch["bottom"] - top))),
                    ]
                    digital.append(
                        dict(
                            text=ch.get("text", ""),
                            x0=box[0],
                            top=box[1],
                            x1=box[2],
                            bottom=box[3],
                            frame=fid,
                            polygon=rectangle(box),
                            conf=None,
                            sequence=sequence,
                            size=millipoints(ch.get("size", ch["bottom"] - ch["top"])),
                            fontname=ch.get("fontname", ""),
                        )
                    )
                ocr_rules = None
                chosen = digital
                alternatives = []
                method = "digital"
                reason = None
                conflict = visibility_conflict(page)
                if conflict:
                    reason = issue(
                        "VISIBILITY_CONFLICT", "blocking", exposure="restricted"
                    )
                if m["suspect_observations"]:
                    issue(
                        "TEXT_LAYER_SUSPECT",
                        measurements={
                            "suspect_observations": m["suspect_observations"]
                        },
                    )
                if m["route"] == "ocr" and not conflict:
                    if runtime["ocr_binary_sha256"] is None:
                        issue("OCR_UNAVAILABLE", "blocking")
                        result["coverage"] = "partial" if digital else "unreadable"
                    elif not set(profile.languages) <= set(
                        t["language"] for t in runtime["traineddata"]
                    ):
                        issue("OCR_LANGUAGE_UNAVAILABLE", "blocking")
                        result["coverage"] = "partial" if digital else "unreadable"
                    else:
                        import pytesseract

                        pytesseract.pytesseract.tesseract_cmd = find_tesseract()
                        image = page.to_image(resolution=profile.dpi).original.convert(
                            "L"
                        )
                        rf = f"p{n:04d}_render"
                        from fractions import Fraction

                        result["frames"].append(
                            dict(
                                id=rf,
                                kind="render_pixels",
                                width=image.width,
                                height=image.height,
                                to_page_mpt=matrix(
                                    [
                                        Fraction(width, image.width),
                                        0,
                                        0,
                                        0,
                                        Fraction(height, image.height),
                                        0,
                                        0,
                                        0,
                                        1,
                                    ]
                                ),
                            )
                        )
                        from .raster import prepare

                        image, recognition, rf, mapped_box, ocr_rules = prepare(
                            image, result, profile.dpi
                        )
                        try:
                            raw = pytesseract.image_to_data(
                                recognition,
                                lang="+".join(profile.languages),
                                config=f"--psm {profile.psm}",
                                output_type=pytesseract.Output.DICT,
                                timeout=120,
                            )
                        except RuntimeError as exc:
                            raise EvidenceError(
                                "WORKER_TIMEOUT", "ocr", "OCR did not complete"
                            ) from exc
                        chosen = []
                        rereads = 0
                        for j, text in enumerate(raw["text"]):
                            if not text:
                                continue
                            x, y, w, h = (
                                int(raw[k][j])
                                for k in ("left", "top", "width", "height")
                            )
                            confidence = max(
                                0, min(10000, round(float(raw["conf"][j]) * 100))
                            )
                            box = mapped_box([x, y, x + w, y + h])
                            word = dict(
                                text=text,
                                x0=box[0],
                                x1=box[2],
                                top=box[1],
                                bottom=box[3],
                                frame=rf,
                                polygon=rectangle([x, y, x + w, y + h]),
                                conf=confidence,
                                sequence=j,
                            )
                            chosen.append(word)
                            if confidence < 8000:
                                issue("OCR_TEXT_UNCERTAIN")
                            if (
                                confidence < 8000
                                and text.lower() in ("not", "no", "unless", "except")
                            ) or re.search(r"\d[OoIl]|[OoIl]\d", text):
                                issue("CRITICAL_TOKEN_UNCERTAIN")
                            if not is_numeric_token(text):
                                counters["ineligible"] += 1
                                continue
                            counters["numeric_eligible"] += 1
                            if (
                                not profile.verify_numbers
                                or rereads >= profile.max_rereads_per_page
                                or counters["reread_attempted"]
                                >= profile.max_rereads_per_document
                            ):
                                counters["reread_not_attempted"] += 1
                                key = (
                                    "budget_exhausted"
                                    if profile.verify_numbers
                                    else "disabled"
                                )
                                counters[key] += 1
                                issue(
                                    "VERIFICATION_BUDGET_EXHAUSTED"
                                    if profile.verify_numbers
                                    else "NUMERIC_REREAD_UNAVAILABLE"
                                )
                                continue
                            rereads += 1
                            counters["reread_attempted"] += 1
                            try:
                                again = pytesseract.image_to_string(
                                    image.crop(
                                        (
                                            max(0, x - 4),
                                            max(0, y - 4),
                                            min(image.width, x + w + 4),
                                            min(image.height, y + h + 4),
                                        )
                                    ),
                                    lang="+".join(profile.languages),
                                    config="--psm 8 -c tessedit_char_whitelist=0123456789.,%-+()/:$",
                                    timeout=10,
                                ).strip()
                            except RuntimeError as exc:
                                raise EvidenceError(
                                    "WORKER_TIMEOUT",
                                    "numeric_reread",
                                    "Numeric verification did not complete",
                                ) from exc
                            if not again:
                                counters["reread_unavailable"] += 1
                                issue("NUMERIC_REREAD_UNAVAILABLE")
                            else:
                                counters["reread_completed"] += 1
                                if normalise_number(again) != normalise_number(text):
                                    counters["reread_disagreed"] += 1
                                    rid = issue("NUMERIC_DISAGREEMENT")
                                    alternatives.append((dict(word, text=again), rid))
                        if recognition is not image:
                            recognition.close()
                        image.close()
                        if digital:
                            rid = issue(
                                "TEXT_LAYER_SUSPECT", rule="digital_ocr_candidates"
                            )
                            alternatives.extend((word, rid) for word in digital)
                        method = "ocr"
                if conflict:
                    result["coverage"] = "unreadable"
                    result["feature_status"] = {
                        f: (
                            "unresolved"
                            if f in profile.required_features
                            else "not_requested"
                        )
                        for f in FEATURES
                    }
                elif chosen:
                    result["method"] = (
                        "mixed" if method == "ocr" and digital else method
                    )
                    if "text" in profile.required_features:
                        result["feature_status"]["text"] = "resolved"
                else:
                    result["blank"] = "unknown"
                    result["coverage"] = "unreadable"
                    # A rendered all-white control plus absence of source objects is a
                    # deterministic blank test, unlike absence of extracted text.
                    if (
                        not page.chars
                        and not page.images
                        and not page.rects
                        and not page.curves
                        and not page.lines
                        and not page.annots
                    ):
                        image = page.to_image(resolution=profile.dpi).original.convert(
                            "L"
                        )
                        low, high = image.getextrema()
                        image.close()
                        if low == 255:
                            result["blank"] = "blank_by_rule"
                            result["coverage"] = "complete"
                            issue(
                                "BLANK_PAGE_ASSESSED",
                                "info",
                                rule="white_raster_no_objects_1",
                                measurements={
                                    "minimum_pixel": low,
                                    "maximum_pixel": high,
                                },
                            )
                    if result["coverage"] == "unreadable":
                        issue("UNREADABLE_REGION", "blocking")
                from .raster import CellInk

                with CellInk(page, result, profile.dpi) as cell_ink:
                    metadata.update(
                        build_page(
                            d,
                            result,
                            page,
                            chosen,
                            alternatives,
                            limits=limits,
                            offsets=offsets,
                            issue=issue,
                            method=method,
                            conflict=conflict,
                            reason=reason,
                            ocr_rules=ocr_rules,
                            cell_ink=cell_ink,
                        )
                    )
                text_count += sum(
                    len(block["observed"]["text"]) for block in d["blocks"].values()
                )
                limits.check("text_codepoints", text_count)
                page.close()
                original.close()
                if progress:
                    progress(
                        dict(
                            stage="extract",
                            page=n,
                            completed_pages=requested.index(n) + 1,
                            requested_pages=len(requested),
                        )
                    )
                spool.put(d, metadata)
            metadata = spool.restore(d)
        finalize_document(d, metadata)
        merge_paragraphs(d, metadata)
        if profile.asset_mode != "none":
            from .assets import render_page_assets

            for result in d["pages"]:
                original = pdf.pages[result["number"] - 1]
                page, _ = _page(original, limits, profile)
                render_page_assets(d, page, result, assets, limits)
                limits.check(
                    "disk_bytes", len(data) + sum(len(v) for v in assets.values())
                )
                page.close()
                original.close()
        d["quality"] = summarize(d, counters)
        validate_document(d)
        return d
