"""Generate the published DEE-1 schema; keep the generated file in the wheel."""

import json
from pathlib import Path

I = {"type": "integer", "minimum": 0, "maximum": 2**53 - 1}
P = dict(I, minimum=1)
S = {"type": "string"}
B = {"type": "boolean"}
D = dict(S, pattern="^[0-9a-f]{64}$")
ID = dict(S, pattern="^[a-z][a-z0-9_-]{0,95}$")


def enum(s):
    return {"enum": s.split()}


def nullable(v):
    return {"anyOf": [v, {"type": "null"}]}


def arr(v, **kw):
    return dict(type="array", items=v, **kw)


def obj(**props):
    return dict(
        type="object",
        properties=props,
        required=list(props),
        additionalProperties=False,
    )


def ref(name):
    return {"$ref": "#/$defs/" + name}


def records(v):
    return {"type": "object", "propertyNames": ID, "additionalProperties": v}


def fixed(v, n):
    return arr(v, minItems=n, maxItems=n)


R = fixed(I, 2)
POINT = fixed(dict(I, minimum=-(2**53 - 1)), 2)
POLY = arr(POINT, minItems=3)
IDS = arr(ID, uniqueItems=True)
EX = {
    "type": "object",
    "propertyNames": {"pattern": "^[a-z][a-z0-9_-]*:[a-z][a-z0-9_-]*$"},
    "additionalProperties": True,
}
d = {}
d["Rational"] = obj(numerator=dict(I, minimum=-(2**53 - 1)), denominator=P)
d["TextMap"] = obj(
    output_range=R,
    observation_id=nullable(ID),
    observation_range=nullable(R),
    transform_id=nullable(ID),
)
d["TextView"] = obj(text=S, map=arr(ref("TextMap")))
d["Frame"] = obj(
    id=ID,
    kind=enum("pdf_user_space_mpt render_pixels deskew_pixels page_mpt"),
    width=P,
    height=P,
    to_page_mpt=fixed(ref("Rational"), 9),
)
d["Page"] = obj(
    number=P,
    printed_label=nullable(S),
    width_mpt=P,
    height_mpt=P,
    coverage=enum("complete partial unreadable"),
    method=enum("digital ocr mixed none"),
    blank=enum("not_blank blank_by_rule unknown"),
    block_order=IDS,
    issue_ids=IDS,
    feature_status=obj(
        **{
            k: enum("resolved unresolved not_present not_requested")
            for k in ("text", "table_structure", "visual_regions")
        }
    ),
    frames=arr(ref("Frame"), minItems=1),
)
d["Observation"] = obj(
    page=P,
    method=enum("digital ocr"),
    frame_id=ID,
    polygon=POLY,
    text=S,
    confidence_bp=nullable(dict(I, maximum=10000)),
    disposition=enum("emitted alternative suppressed_duplicate unresolved restricted"),
    owner_block_id=nullable(ID),
    reason_issue_ids=IDS,
)
d["SourceSpan"] = obj(
    page=P, polygon_mpt=POLY, bbox_mpt=fixed(I, 4), observation_ids=IDS
)
d["TextTransform"] = obj(
    rule_id=ID,
    rule_version=S,
    kind=enum("structural normalization inference"),
    input=arr(obj(observation_id=ID, range=R)),
    before=S,
    after=S,
    reason_code=S,
    source_span_ids=IDS,
)
codes = "OCR_UNAVAILABLE OCR_LANGUAGE_UNAVAILABLE TEXT_LAYER_SUSPECT OCR_TEXT_UNCERTAIN NUMERIC_DISAGREEMENT CRITICAL_TOKEN_UNCERTAIN VERIFICATION_BUDGET_EXHAUSTED NUMERIC_REREAD_UNAVAILABLE READING_ORDER_UNCERTAIN TABLE_STRUCTURE_UNCERTAIN UNREADABLE_REGION UNSUPPORTED_FEATURE VISIBILITY_CONFLICT ASSET_UNAVAILABLE NORMALIZATION_INFERRED EQUATION_UNINTERPRETED BLANK_PAGE_ASSESSED"
d["Issue"] = obj(
    code=enum(codes),
    severity=enum("info warning blocking"),
    scope=enum("document page block span observation cell"),
    target_ids=arr(S, uniqueItems=True),
    page=P,
    details=obj(rule_id=S, measurements={"type": "object", "additionalProperties": I}),
    suggested_action=enum("review_source provide_ocr provide_transcription none"),
    exposure=enum("ordinary restricted"),
)
d["Issue"]["required"].remove("page")
d["Cell"] = obj(
    id=ID,
    row=I,
    column=I,
    row_span=P,
    column_span=P,
    header_role=enum("column row none unknown"),
    value_state=enum("text blank unreadable absent"),
    observed=ref("TextView"),
    reading=ref("TextView"),
    source_span_ids=IDS,
    region_span_id=ID,
    issue_ids=IDS,
)
payloads = {
    "paragraph": obj(),
    "callout": obj(),
    "heading": obj(
        level=nullable(dict(P, maximum=6)),
        literal_label=nullable(S),
        basis=enum("typography numbering unknown"),
    ),
    "list_item": obj(marker=S, depth=nullable(I)),
    "page_furniture": obj(role=enum("header footer page_number unknown")),
    "figure": obj(asset_name=nullable(S), caption_block_ids=IDS),
    "equation": obj(
        representation=enum("reconstructed_latex uninterpreted_glyphs image"),
        latex=nullable(S),
        asset_name=nullable(S),
    ),
    "unresolved_region": obj(reason_code=enum(codes), asset_name=nullable(S)),
    "table": obj(
        rows=P, columns=P, cells=arr(ref("Cell")), structure=enum("resolved candidate")
    ),
}
d["Block"] = obj(
    page=P,
    type=enum(" ".join(payloads)),
    source_span_ids=IDS,
    observed=ref("TextView"),
    reading=ref("TextView"),
    issue_ids=IDS,
    parent_id=nullable(ID),
    relations=arr(
        obj(
            kind=enum("continues caption_of footnote_of references"),
            target_id=ID,
            certainty=enum("rule_supported candidate"),
        )
    ),
    payload={},
)
d["Block"]["allOf"] = [
    {
        "if": {"properties": {"type": {"const": k}}},
        "then": {"properties": {"payload": v}},
    }
    for k, v in payloads.items()
]
d["Quality"] = obj(
    coverage=enum("complete partial unreadable"),
    **{
        k: arr(P, uniqueItems=True)
        for k in (
            "requested_pages",
            "complete_pages",
            "partial_pages",
            "unreadable_pages",
            "blank_pages",
        )
    },
    issue_counts_by_code={
        "type": "object",
        "propertyNames": enum(codes),
        "additionalProperties": I,
    },
    issue_counts_by_severity=obj(info=I, warning=I, blocking=I),
    unresolved_region_count=I,
    verification=obj(
        **{
            k: I
            for k in (
                "numeric_eligible",
                "reread_completed",
                "reread_unavailable",
                "reread_not_attempted",
                "reread_attempted",
                "reread_disagreed",
                "disabled",
                "budget_exhausted",
                "ineligible",
            )
        }
    ),
)
limits = "input_bytes pages page_content_bytes page_pixels text_codepoints blocks observations spans issues document_bytes artifact_bytes disk_bytes"
d["EffectiveRecipe"] = obj(
    schema_version={"const": "1.0.0"},
    engine_build_sha256=D,
    profile_id={"const": "evidence-conservative-1"},
    routing_policy_id={"const": "auto-1"},
    normalization_policy_id={"const": "conservative-1"},
    requested_pages=arr(P, minItems=1, uniqueItems=True),
    required_features=arr(
        enum("text table_structure visual_regions"), minItems=1, uniqueItems=True
    ),
    ocr=obj(
        mode=enum("digital_only ocr_all auto"),
        languages=arr(S, minItems=1, uniqueItems=True),
        dpi=dict(I, minimum=72, maximum=1200),
        psm=enum("unused"),
        verify_numbers=B,
        max_rereads_per_page=I,
        max_rereads_per_document=I,
    ),
    rules={"type": "object", "additionalProperties": obj(version=S, sha256=D)},
    runtime=obj(
        qualification_id=S,
        python_version=S,
        os=S,
        architecture=S,
        dependencies=arr(obj(name=S, version=S, sha256=D)),
        ocr_binary_sha256=nullable(D),
        traineddata=arr(obj(language=S, sha256=D)),
    ),
    count_limits=obj(**{k: P for k in limits.split()}),
    exports=arr(
        {
            "oneOf": [
                obj(kind=enum("document markdown issues"), required=B, options=obj()),
                obj(
                    kind={"const": "png"},
                    required={"const": True},
                    options=obj(scope=enum("figures regions")),
                ),
            ]
        }
    ),
)
d["EffectiveRecipe"]["properties"]["ocr"]["properties"]["psm"] = {
    "enum": [1, *range(3, 14)]
}
d["DocumentResult"] = obj(
    schema_version={"const": "1.0.0"},
    source=obj(sha256=D, mime_type={"const": "application/pdf"}, bytes=P),
    extraction_id=D,
    recipe_hash=D,
    recipe=ref("EffectiveRecipe"),
    scope=obj(
        total_pages=P,
        requested_pages=arr(P, minItems=1, uniqueItems=True),
        required_features=arr(
            enum("text table_structure visual_regions"), uniqueItems=True
        ),
    ),
    pages=arr(ref("Page"), minItems=1),
    **{
        k: records(ref(t))
        for k, t in [
            ("observations", "Observation"),
            ("blocks", "Block"),
            ("spans", "SourceSpan"),
            ("transforms", "TextTransform"),
            ("issues", "Issue"),
        ]
    },
    quality=ref("Quality"),
    extensions=EX,
)
d["Manifest"] = obj(
    schema_version={"const": "1.0.0"},
    extraction_id=D,
    document_hash=D,
    artifacts=arr(
        obj(
            name=S,
            mime_type=enum("application/json text/markdown image/png"),
            bytes=I,
            sha256=D,
            exposure=enum("ordinary restricted"),
        )
    ),
)
schema = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:dee:document:1.0.0",
    "$ref": "#/$defs/DocumentResult",
    "$defs": d,
}
if __name__ == "__main__":
    Path(__file__).with_name("document-1.0.0.json").write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
    )
