import copy
import io
from collections import Counter

import pytest
from reportlab.pdfgen import canvas
from pdf_to_md.evidence import (
    extract_document,
    Profile,
    EvidenceError,
    validate_document,
    render_markdown,
)


def drawing(draw):
    stream = io.BytesIO()
    c = canvas.Canvas(stream, pagesize=(612, 792), invariant=1)
    draw(c)
    c.save()
    stream.seek(0)
    return stream


def table(c, *, merged=False, blank=False, bold=False, top=650):
    xs = [72, 180, 300]
    ys = [top, top - 30, top - 60, top - 90]
    for y in ys:
        c.line(xs[0], y, xs[-1], y)
    for x in (xs[0], xs[-1]):
        c.line(x, ys[0], x, ys[-1])
    c.line(xs[1], ys[1] if merged else ys[0], xs[1], ys[-1])
    for r, values in enumerate([["Group", "Value"], ["Alpha", "10"], ["Beta", "20"]]):
        if blank and r == 1:
            continue
        c.setFont("Helvetica-Bold" if bold and r == 0 else "Helvetica", 10)
        for col, text in enumerate(values):
            if merged and r == 0 and col == 1:
                continue
            c.drawString(xs[col] + 5, ys[r] - 20, text)


def extract(draw, **options):
    return extract_document(
        drawing(draw), profile=Profile(mode="digital_only", **options)
    )


def blocks(d, kind):
    return [b for b in d["blocks"].values() if b["type"] == kind]


@pytest.mark.parametrize(
    "merged,blank,bold",
    [
        (False, False, False),
        (True, False, False),
        (False, True, False),
        (False, False, True),
    ],
)
def test_ruled_tables_preserve_grid_and_literals(merged, blank, bold):
    d = extract(lambda c: table(c, merged=merged, blank=blank, bold=bold))
    t = blocks(d, "table")[0]
    p = t["payload"]
    assert (p["rows"], p["columns"], p["structure"]) == (3, 2, "resolved")
    assert len(p["cells"]) == 5 if merged else len(p["cells"]) == 6
    assert p["cells"][0]["column_span"] == (2 if merged else 1)
    if blank:
        assert [c["value_state"] for c in p["cells"][2:4]] == ["blank", "blank"]
    if not bold:
        assert all(c["header_role"] == "unknown" for c in p["cells"])
    else:
        assert [c["header_role"] for c in p["cells"][:2]] == ["column", "column"]
    emitted = [
        o["text"] for o in d["observations"].values() if o["disposition"] == "emitted"
    ]
    assert Counter("".join(emitted)) == Counter(
        t["observed"]["text"].replace("\t", "").replace("\n", "")
    )
    validate_document(d)


def test_lost_cell_and_row_are_detected():
    d = extract(lambda c: table(c, blank=True))
    mutated = copy.deepcopy(d)
    t = blocks(mutated, "table")[0]
    t["payload"]["cells"].pop(2)
    with pytest.raises(EvidenceError):
        validate_document(mutated)
    mutated = copy.deepcopy(d)
    blocks(mutated, "table")[0]["reading"]["text"] = "changed"
    with pytest.raises(EvidenceError):
        validate_document(mutated)


def test_borderless_candidate_preserves_rows():
    def draw(c):
        c.line(72, 650, 300, 650)
        c.line(72, 550, 300, 550)
        for row in range(3):
            c.drawString(80, 630 - row * 30, "Item " + str(row))
            c.drawString(230, 630 - row * 30, str(row * 10))

    d = extract(draw)
    t = blocks(d, "table")[0]
    assert t["payload"]["structure"] == "candidate"
    assert t["payload"]["rows"] == 3
    assert "TABLE_STRUCTURE_UNCERTAIN" in d["quality"]["issue_counts_by_code"]


def test_shaded_background_is_callout_but_overlay_is_withheld():
    def panel(c):
        c.setFillGray(0.8)
        c.rect(72, 500, 350, 80, fill=1, stroke=0)
        c.setFillGray(0)
        c.drawString(85, 550, "Literal callout text.")
        c.drawString(85, 530, "Second callout line.")

    d = extract(panel)
    assert "Literal callout text." in blocks(d, "callout")[0]["observed"]["text"]
    assert "VISIBILITY_CONFLICT" not in d["quality"]["issue_counts_by_code"]

    def overlay(c):
        c.drawString(85, 550, "Hidden text")
        c.setFillGray(1)
        c.rect(72, 500, 350, 80, fill=1, stroke=0)

    d = extract(overlay)
    assert "VISIBILITY_CONFLICT" in d["quality"]["issue_counts_by_code"]
    assert "Hidden text" not in render_markdown(d)


def test_heading_list_furniture_and_numbering():
    def draw(c):
        for n in (1, 2):
            c.setFont("Helvetica", 9)
            c.drawString(72, 765, "Repeated source header")
            c.drawString(290, 25, str(n))
            c.setFont("Helvetica-Bold", 18)
            c.drawString(72, 700, "1. Literal Heading")
            c.setFont("Helvetica", 11)
            c.drawString(72, 660, "Body paragraph with meaningful hyphen-")
            c.drawString(72, 644, "ated source content.")
            c.drawString(72, 600, "(a) First literal item")
            c.drawString(100, 582, "(i) Nested literal item")
            c.showPage()

    d = extract(draw)
    assert len(blocks(d, "heading")) == 2
    assert [b["payload"]["marker"] for b in blocks(d, "list_item")] == [
        "(a)",
        "(i)",
        "(a)",
        "(i)",
    ]
    assert [b["payload"]["depth"] for b in blocks(d, "list_item")] == [0, 1, 0, 1]
    assert len(blocks(d, "page_furniture")) == 4
    assert [p["printed_label"] for p in d["pages"]] == ["1", "2"]


def test_table_continuation_is_candidate_and_page_local():
    def draw(c):
        table(c, top=200)
        c.showPage()
        table(c, top=720)
        c.showPage()

    d = extract(draw)
    ts = blocks(d, "table")
    assert ts[0]["relations"][0]["kind"] == "continues"
    assert ts[0]["relations"][0]["certainty"] == "candidate"
    assert all(
        o["page"] == d["blocks"][o["owner_block_id"]]["page"]
        for o in d["observations"].values()
        if o["owner_block_id"]
    )


def figure(c):
    from PIL import Image
    from reportlab.lib.utils import ImageReader

    c.drawImage(ImageReader(Image.new("RGB", (50, 50), "blue")), 72, 400, 180, 120)
    c.setFont("Helvetica", 10)
    c.drawString(72, 380, "Figure 1: Literal caption.")
    c.drawString(72, 350, "Body following the figure.")


def test_figure_caption_and_package_roundtrip(tmp_path):
    from pdf_to_md.evidence import extract_package, write_bundle, read_bundle
    from pdf_to_md.evidence.canonical import canonical

    package = extract_package(
        drawing(figure), profile=Profile(mode="digital_only", asset_mode="figures")
    )
    d = package.document
    f = blocks(d, "figure")[0]
    assert len(f["payload"]["caption_block_ids"]) == 1
    caption = d["blocks"][f["payload"]["caption_block_ids"][0]]
    assert caption["observed"]["text"] == "Figure 1: Literal caption."
    assert "Figure 1" not in f["observed"]["text"]
    assert set(package.assets) == {f["payload"]["asset_name"]}
    with pytest.raises(EvidenceError):
        write_bundle(d, tmp_path / "missing")
    receipt = write_bundle(d, tmp_path / "bundle", assets=package.assets)
    loaded = read_bundle(tmp_path / "bundle")
    assert (
        canonical(loaded.document) == canonical(d) and loaded.assets == package.assets
    )
    assert (
        write_bundle(loaded.document, tmp_path / "again", assets=loaded.assets)[
            "manifest_hash"
        ]
        == receipt["manifest_hash"]
    )
    assert "![" in render_markdown(d)


def test_restricted_crop_cannot_be_marked_ordinary(tmp_path):
    import json
    from pdf_to_md.evidence import extract_package, write_bundle, validate_bundle
    from pdf_to_md.evidence.canonical import canonical

    def hidden(c):
        t = c.beginText(72, 700)
        t.setTextRenderMode(3)
        t.textLine("Restricted candidate")
        c.drawText(t)

    package = extract_package(drawing(hidden), profile=Profile(asset_mode="regions"))
    write_bundle(package.document, tmp_path / "bundle", assets=package.assets)
    path = tmp_path / "bundle" / "manifest.json"
    manifest = json.loads(path.read_bytes())
    pngs = [e for e in manifest["artifacts"] if e["name"].endswith(".png")]
    assert pngs and all(e["exposure"] == "restricted" for e in pngs)
    assert "![" not in render_markdown(package.document)
    pngs[0]["exposure"] = "ordinary"
    path.write_bytes(canonical(manifest))
    with pytest.raises(EvidenceError):
        validate_bundle(tmp_path / "bundle")


def test_paragraph_join_keeps_hyphen_and_records_line_change():
    def draw(c):
        c.setFont("Helvetica", 11)
        c.drawString(72, 650, "Meaningful hyphen-")
        c.drawString(72, 634, "ated source word.")

    d = extract(draw)
    p = blocks(d, "paragraph")[0]
    assert p["observed"]["text"] == "Meaningful hyphen-\nated source word."
    assert p["reading"]["text"] == "Meaningful hyphen- ated source word."
    assert any(t["rule_id"] == "paragraph_join" for t in d["transforms"].values())


def test_table_cell_cannot_steal_another_cell_observation():
    d = extract(table)
    t = blocks(d, "table")[0]
    t["payload"]["cells"][1]["source_span_ids"] = t["payload"]["cells"][0][
        "source_span_ids"
    ]
    with pytest.raises(EvidenceError):
        validate_document(d)


def test_border_crossing_cannot_be_certified_resolved():
    def draw(c):
        table(c)
        c.setFont("Helvetica", 10)
        c.drawString(168, 600, "crossing boundary")

    d = extract(draw)
    t = blocks(d, "table")[0]
    assert t["payload"]["structure"] == "candidate"
    t["payload"]["structure"] = "resolved"
    with pytest.raises(EvidenceError) as error:
        validate_document(d)
    assert "resolved cell crosses" in str(error.value.__cause__)


def test_regions_export_final_paragraph_once_and_are_deterministic(tmp_path):
    from pdf_to_md.evidence import extract_package, write_bundle
    from pdf_to_md.evidence.canonical import canonical

    def draw(c):
        for page in range(2):
            c.setFont("Helvetica", 11)
            for line in range(10):
                c.drawString(
                    72, 650 - line * 16, "Literal continuous paragraph content."
                )
            c.showPage()

    profile = Profile(mode="digital_only", asset_mode="regions")
    a = extract_package(drawing(draw), profile=profile)
    b = extract_package(drawing(draw), profile=profile)
    assert len(blocks(a.document, "paragraph")) == 2
    assert len(a.assets) == 2
    assert a.assets == b.assets and canonical(a.document) == canonical(b.document)
    write_bundle(a.document, tmp_path / "regions", assets=a.assets)


def test_unclassified_curve_has_located_gap():
    def draw(c):
        c.drawString(72, 650, "Source text")
        c.circle(400, 300, 3, fill=0)

    d = extract(draw)
    assert d["quality"]["coverage"] == "partial"
    gaps = blocks(d, "unresolved_region")
    assert gaps
    assert any(
        d["spans"][b["source_span_ids"][0]]["bbox_mpt"][2]
        - d["spans"][b["source_span_ids"][0]]["bbox_mpt"][0]
        < 10000
        for b in gaps
    )


def test_vertical_merged_cell_preserves_row_span():
    def draw(c):
        for x in (72, 180, 300):
            c.line(x, 650, x, 560)
        for y in (650, 590, 560):
            c.line(72, y, 300, y)
        c.line(180, 620, 300, 620)
        for x, y, text in [
            (80, 630, "Merged"),
            (190, 630, "A"),
            (190, 600, "B"),
            (80, 570, "Last"),
            (190, 570, "C"),
        ]:
            c.drawString(x, y, text)

    d = extract(draw)
    t = blocks(d, "table")[0]
    assert t["payload"]["cells"][0]["row_span"] == 2
    assert t["payload"]["structure"] == "resolved"


def test_equations_keep_observations_separate_from_inferred_latex():
    from pathlib import Path

    path = Path(__file__).parents[1] / "fixtures" / "equations.pdf"
    with path.open("rb") as source:
        d = extract_document(source)
    equations = blocks(d, "equation")
    assert equations
    for b in equations:
        p = b["payload"]
        if p["representation"] == "reconstructed_latex":
            assert p["latex"]
            assert any(
                d["issues"][i]["code"] == "NORMALIZATION_INFERRED"
                for i in b["issue_ids"]
            )
        else:
            assert p["latex"] is None
        assert b["observed"]["text"] == b["reading"]["text"]
        assert all(
            m["observation_id"] is not None or m["transform_id"] is not None
            for m in b["observed"]["map"]
        )


def test_page_spool_tamper_refuses_and_cleans_up():
    from pathlib import Path
    from pdf_to_md.evidence.spool import PageSpool
    from pdf_to_md.evidence.structure import FAMILIES
    from pdf_to_md.evidence import Limits

    with PageSpool(Limits(), 0) as spool:
        directory = Path(spool.directory.name)
        d = dict(pages=[], **{k: {} for k in FAMILIES})
        spool.put(d, {})
        spool.records[0][0].write_bytes(b"{}")
        with pytest.raises(EvidenceError):
            spool.restore(d)
    assert not directory.exists()


def test_ocr_inverse_affine_is_canonical_and_localizes_pixels(monkeypatch):
    from PIL import Image
    from pdf_to_md.evidence import raster
    from pdf_to_md.evidence.canonical import canonical
    from pdf_to_md.evidence.coordinates import project, rectangle

    monkeypatch.setattr(raster, "_deskew_with_angle", lambda img: (img, 2.125))
    monkeypatch.setattr(
        raster, "_grid_recognition_image", lambda img, dpi: (img.copy(), [], [])
    )
    page = dict(number=1, width_mpt=612000, height_mpt=792000, frames=[])
    image, recognition, fid, mapped, _ = raster.prepare(
        Image.new("RGB", (2550, 3300)), page, 300
    )
    assert fid.endswith("_deskew")
    canonical(page["frames"])
    polygon, _ = project(
        rectangle([1275, 1650, 1276, 1651]), page["frames"][0]["to_page_mpt"]
    )
    assert abs(polygon[0][0] - 306000) <= 1 and abs(polygon[0][1] - 396000) <= 1
    image.close()
    recognition.close()


def test_cell_geometry_cannot_expand_to_table_region():
    d = extract(table)
    t = blocks(d, "table")[0]
    broad = max(
        t["source_span_ids"],
        key=lambda sid: (
            d["spans"][sid]["bbox_mpt"][2] - d["spans"][sid]["bbox_mpt"][0]
        )
        * (d["spans"][sid]["bbox_mpt"][3] - d["spans"][sid]["bbox_mpt"][1]),
    )
    cell = t["payload"]["cells"][0]
    cell["source_span_ids"].append(broad)
    # List position cannot change the explicitly bound cell geometry.
    validate_document(d)
    cell["region_span_id"] = broad
    with pytest.raises(EvidenceError) as error:
        validate_document(d)
    assert "cell grid boundary" in str(error.value.__cause__)


@pytest.mark.parametrize("primitive", ["line", "rectangle"])
def test_rejected_small_visual_primitive_is_not_complete(primitive):
    def draw(c):
        c.drawString(72, 650, "Literal source")
        if primitive == "line":
            c.line(400, 300, 404, 304)
        else:
            c.rect(400, 300, 4, 4, fill=1)

    d = extract(draw)
    assert d["quality"]["coverage"] == "partial"
    assert blocks(d, "unresolved_region")


def test_callout_can_contain_table_without_reclaiming_observations():
    def draw(c):
        c.setFillGray(0.9)
        c.rect(60, 540, 270, 160, fill=1, stroke=0)
        c.setFillGray(0)
        c.drawString(72, 680, "Panel introduction")
        table(c)

    d = extract(draw)
    t = blocks(d, "table")[0]
    parent = d["blocks"][t["parent_id"]]
    assert parent["type"] == "callout"
    assert parent["observed"]["text"] == "Panel introduction"
    assert "Alpha" not in parent["observed"]["text"]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_cropped_table_keeps_bounded_cell_geometry(rotation):
    def draw(c):
        c.setPageRotation(rotation)
        c.setCropBox(
            (30, 40, 740, 580) if rotation in (90, 270) else (30, 40, 580, 740)
        )
        table(c, top=500)

    d = extract(draw)
    assert blocks(d, "table")
    page = d["pages"][0]
    for span in d["spans"].values():
        x0, y0, x1, y1 = span["bbox_mpt"]
        assert 0 <= x0 < x1 <= page["width_mpt"]
        assert 0 <= y0 < y1 <= page["height_mpt"]
    validate_document(d)


def test_scanned_grid_preserves_empty_ink_as_unreadable():
    from pathlib import Path
    from pdf_to_md.ocr import find_tesseract

    if not find_tesseract():
        pytest.skip("Tesseract required for real scanned table control")
    path = Path(__file__).parents[1] / "fixtures" / "scanned_science.pdf"
    if not path.exists():
        from make_scanned_fixture import build_all

        build_all()
    with path.open("rb") as source:
        d = extract_document(source)
    t = blocks(d, "table")[0]
    assert t["payload"]["structure"] == "candidate"
    states = Counter(c["value_state"] for c in t["payload"]["cells"])
    assert states["blank"] and states["unreadable"] and states["text"]
    assert not blocks(d, "heading")
    assert d["quality"]["coverage"] == "partial"
