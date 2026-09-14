import copy
import io
import json
import subprocess
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas
from pdf_to_md.evidence import (
    EvidenceError,
    Limits,
    Profile,
    extract_document,
    inspect_document,
    render_markdown,
    validate_document,
    write_bundle,
    validate_bundle,
)
from pdf_to_md.evidence.canonical import canonical, decode, digest
from pdf_to_md.evidence.coordinates import matrix, project


def pdf(text="Literal not less than 30 days.", *, hidden=False, blank=False):
    stream = io.BytesIO()
    c = canvas.Canvas(stream, invariant=1)
    if hidden:
        t = c.beginText(72, 720)
        t.setTextRenderMode(3)
        t.textLine(text)
        c.drawText(t)
    elif not blank:
        c.drawString(72, 720, text)
    c.showPage()
    c.save()
    stream.seek(0)
    return stream


@pytest.fixture(scope="module")
def document():
    return extract_document(pdf())


def test_canonical_golden():
    value = {
        "\U00010000": "astral",
        "\ue000": "bmp",
        "controls": "\x00\b\t\n\f\r\x1f",
        "text": "e\u0301 😀",
        "number": 2**53 - 1,
    }
    expected = b'{"controls":"\\u0000\\b\\t\\n\\f\\r\\u001f","number":9007199254740991,"text":"e\xcc\x81 \xf0\x9f\x98\x80","\xee\x80\x80":"bmp","\xf0\x90\x80\x80":"astral"}\n'
    assert canonical(value) == expected
    js = Path(__file__).parents[2] / "src/pdf_to_md/evidence/schema/canonical.mjs"
    code = f'import {{canonical,sliceCodepoints}} from {json.dumps(js.as_uri())};console.log(canonical({json.dumps(value)}).toString("hex"));console.log(sliceCodepoints("a😀é",1,3));'
    result = subprocess.run(
        ["node", "--input-type=module", "-e", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [expected.hex(), "😀e"]


@pytest.mark.parametrize(
    "bad", [b'{"a":1,"a":2}', b"-0", b"1.0", b"NaN", b"9007199254740992", b'"\\ud800"']
)
def test_decode_refuses(bad):
    with pytest.raises(ValueError):
        decode(bad)


@pytest.mark.parametrize(
    "bad", [1.5, float("nan"), {"x": "\udfff"}, 2**53, {"x": (1, 2)}]
)
def test_encoder_refuses(bad):
    with pytest.raises(ValueError):
        canonical(bad)


def test_coordinates():
    from fractions import Fraction

    transform = matrix([0, Fraction(3, 2), -10, -2, 0, 100, 0, 0, 1])
    polygon, box = project([[0, 1], [1, 1], [1, 3]], transform)
    assert polygon == [[-8, 100], [-8, 98], [-6, 98]]
    assert box == [-9, 98, -5, 100]
    with pytest.raises(ValueError):
        project([[1, 2]], matrix([0] * 9))


def test_preserves_source_and_determinism(document):
    stream = pdf()
    original = stream.getvalue()
    assert canonical(extract_document(stream)) == canonical(document)
    assert not stream.closed and stream.getvalue() == original
    assert document["source"]["sha256"] == digest(original)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda d: d["blocks"]["b00000001"]["observed"]["map"].pop(),
        lambda d: d["observations"].pop(next(iter(d["observations"]))),
        lambda d: d["observations"][next(iter(d["observations"]))].update(
            owner_block_id=None
        ),
        lambda d: d["blocks"]["b00000001"]["reading"].update(text="altered"),
        lambda d: d["quality"].update(coverage="partial"),
        lambda d: d["pages"][0]["frames"][0].update(to_page_mpt=matrix([0] * 9)),
        lambda d: d["pages"][0]["block_order"].clear(),
        lambda d: d["blocks"]["b00000001"].update(parent_id="b00000001"),
        lambda d: d["recipe"].update(profile_id="unknown"),
        lambda d: d["source"].update(bytes=True),
        lambda d: d.update(job_id="operational"),
    ],
)
def test_semantic_negative_controls(document, mutation):
    d = copy.deepcopy(document)
    mutation(d)
    with pytest.raises(EvidenceError):
        validate_document(d)


def test_bundle_integrity_and_replay(document, tmp_path):
    target = tmp_path / "bundle"
    first = write_bundle(document, target)
    assert write_bundle(document, target) == first
    assert validate_bundle(target)["manifest_hash"] == first["manifest_hash"]
    (target / "reading.md").write_text("corrupted")
    with pytest.raises(EvidenceError):
        validate_bundle(target)


def test_bundle_symlink(document, tmp_path):
    target = tmp_path / "bundle"
    write_bundle(document, target)
    (target / "reading.md").unlink()
    (target / "reading.md").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text("unrelated")
    with pytest.raises(EvidenceError):
        validate_bundle(target)


def test_hidden_text_withheld(tmp_path):
    d = extract_document(pdf("restricted secret", hidden=True))
    assert d["quality"]["coverage"] == "unreadable"
    assert "restricted secret" not in render_markdown(d)
    target = tmp_path / "bundle"
    write_bundle(d, target)
    assert b"restricted secret" not in (target / "issues.json").read_bytes()
    assert any(o["disposition"] == "restricted" for o in d["observations"].values())


def test_blank_is_distinct():
    d = extract_document(pdf(blank=True))
    assert d["quality"]["blank_pages"] == [1]
    assert d["quality"]["coverage"] == "complete"


def test_safe_markdown():
    d = extract_document(pdf("<script> ![image](https://example.test/x)"))
    rendered = render_markdown(d)
    assert "<script>" not in rendered and "![image](" not in rendered


def test_limits_and_options():
    for kwargs in ({"dpi": True}, {"requested_pages": (True,)}, {"mode": "other"}):
        with pytest.raises(EvidenceError):
            Profile(**kwargs)
    with pytest.raises(EvidenceError) as error:
        extract_document(pdf(), limits=Limits(input_bytes=10))
    assert error.value.code == "LIMIT_EXCEEDED"
    with pytest.raises(EvidenceError):
        extract_document(pdf(), profile=Profile(requested_pages=(2,)))


def test_inspection_is_not_completion():
    stream = pdf()
    inspection = inspect_document(stream, limits=Limits())
    assert inspection["page_count"] == 1 and "quality" not in inspection
    assert not stream.closed


def test_schema_and_types_are_current():
    import runpy

    root = Path(__file__).parents[2] / "src/pdf_to_md/evidence/schema"
    builder = runpy.run_path(str(root / "build.py"))
    assert json.loads((root / "document-1.0.0.json").read_text()) == builder["schema"]
    types = runpy.run_path(str(root / "generate_types.py"))
    assert (root / "document-1.0.0.ts").read_text() == types["generate"]()


def test_citation_cannot_lose_observation_geometry(document):
    d = copy.deepcopy(document)
    d["spans"]["s00000001"]["observation_ids"].pop()
    with pytest.raises(EvidenceError):
        validate_document(d)


def test_unresolved_reason_requires_issue(document):
    d = copy.deepcopy(document)
    b = d["blocks"]["b00000001"]
    b.update(
        type="unresolved_region",
        payload={"reason_code": "UNREADABLE_REGION", "asset_name": None},
    )
    with pytest.raises(EvidenceError):
        validate_document(d)


def test_legacy_subcommand_filename(tmp_path, monkeypatch):
    from pdf_to_md.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / "inspect").write_bytes(pdf().getvalue())
    assert main(["inspect", "--stdout"]) == 0


def test_nonseekable_and_requested_pages():
    stream = pdf()

    class Reader:
        def read(self, n):
            return stream.read(n)

    d = extract_document(Reader(), profile=Profile(requested_pages=(1,)))
    assert d["scope"]["requested_pages"] == [1]


def test_missing_ocr_has_gap(monkeypatch):
    import pdf_to_md.evidence.extract as engine

    real = engine.inventory()
    real.update(ocr_binary_sha256=None, traineddata=[])
    monkeypatch.setattr(engine, "inventory", lambda: copy.deepcopy(real))
    d = extract_document(pdf(), profile=Profile(mode="ocr_all"))
    assert d["quality"]["coverage"] == "partial"
    assert "OCR_UNAVAILABLE" in d["quality"]["issue_counts_by_code"]
    assert d["pages"][0]["feature_status"]["text"] == "unresolved"
    assert "Literal" in render_markdown(d)


def test_missing_language_no_substitution(monkeypatch):
    import pdf_to_md.evidence.extract as engine

    real = engine.inventory()
    real.update(ocr_binary_sha256="1" * 64, traineddata=[])
    monkeypatch.setattr(engine, "inventory", lambda: copy.deepcopy(real))
    d = extract_document(pdf(), profile=Profile(mode="ocr_all", languages=("deu",)))
    assert "OCR_LANGUAGE_UNAVAILABLE" in d["quality"]["issue_counts_by_code"]


def test_citation_rechecks_authority_and_exact_text(document):
    from pdf_to_md.evidence.citations import resolve_citation

    text = document["blocks"]["b00000001"]["reading"]["text"]
    citation = dict(
        source_hash=document["source"]["sha256"],
        extraction_id=document["extraction_id"],
        document_hash=digest(canonical(document)),
        block_id="b00000001",
        view="reading",
        range=[0, len(text)],
        view_hash=digest(text.encode()),
    )
    assert (
        resolve_citation(document, citation, authorize=lambda: True, quote=text) == text
    )
    with pytest.raises(EvidenceError):
        resolve_citation(document, citation, authorize=lambda: True, quote="altered")
    with pytest.raises(EvidenceError) as error:
        resolve_citation({}, citation, authorize=lambda: False)
    assert error.value.code == "SOURCE_UNAVAILABLE"
    citation["range"] = [True, 2]
    with pytest.raises(EvidenceError):
        resolve_citation(document, citation, authorize=lambda: True)


def test_inspection_rejects_untrusted_extra_fields():
    from pdf_to_md.evidence.inspection import validate_inspection

    stream = pdf()
    result = inspect_document(stream, limits=Limits())
    validate_inspection(
        result,
        limits=Limits(),
        source_hash=result["source"]["sha256"],
        source_bytes=result["source"]["bytes"],
    )
    result["tool_call"] = "untrusted content"
    with pytest.raises(EvidenceError):
        validate_inspection(
            result,
            limits=Limits(),
            source_hash=result["source"]["sha256"],
            source_bytes=result["source"]["bytes"],
        )
