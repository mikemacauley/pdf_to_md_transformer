"""Independent relationship validation before a bundle can be published."""

import json
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from .canonical import canonical, digest, identity
from .coordinates import coefficients, project
from .model import EvidenceError, Limits
from .quality import summarize


@lru_cache(maxsize=1)
def schema():
    return json.loads(
        Path(__file__).with_name("schema").joinpath("document-1.0.0.json").read_text()
    )


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def _range(r, text):
    require(0 <= r[0] <= r[1] <= len(text), "invalid code point range")
    return text[r[0] : r[1]]


def _validate(d):
    canonical(d)
    Draft202012Validator(schema()).validate(d)
    recipe = d["recipe"]
    require(d["recipe_hash"] == digest(canonical(recipe)), "recipe hash mismatch")
    require(
        d["extraction_id"] == identity(d["source"]["sha256"], d["recipe_hash"]),
        "extraction identity mismatch",
    )
    pages = {p["number"]: p for p in d["pages"]}
    requested = d["scope"]["requested_pages"]
    require(
        requested
        == sorted(set(requested))
        == [p["number"] for p in d["pages"]]
        == recipe["requested_pages"],
        "page accounting mismatch",
    )
    require(max(requested) <= d["scope"]["total_pages"], "page outside source")
    require(
        d["scope"]["required_features"]
        == recipe["required_features"]
        == sorted(recipe["required_features"]),
        "feature scope mismatch",
    )
    obs, blocks, spans, transforms, issues = (
        d[k] for k in ("observations", "blocks", "spans", "transforms", "issues")
    )
    limits = Limits(**recipe["count_limits"])
    for k in ("observations", "blocks", "spans", "issues"):
        limits.check(k, len(d[k]))
    limits.check("input_bytes", d["source"]["bytes"])
    limits.check("pages", d["scope"]["total_pages"])
    limits.check(
        "text_codepoints", sum(len(b["observed"]["text"]) for b in blocks.values())
    )
    limits.check("document_bytes", len(canonical(d)))
    frames = {}
    order = []
    for p in pages.values():
        for f in p["frames"]:
            require(f["id"] not in frames, "duplicate frame id")
            coefficients(f["to_page_mpt"])
            frames[f["id"]] = (p["number"], f)
        require(set(p["issue_ids"]) <= issues.keys(), "unknown page issue")
        for bid in p["block_order"]:
            require(
                bid in blocks and blocks[bid]["page"] == p["number"],
                "invalid block order",
            )
            order.append(bid)
        for feature, status in p["feature_status"].items():
            require(
                (status == "not_requested")
                == (feature not in recipe["required_features"]),
                "feature status contradicts scope",
            )
        if p["coverage"] == "complete":
            require(
                "unresolved" not in p["feature_status"].values(),
                "complete page with feature gap",
            )
        if p["blank"] == "blank_by_rule":
            require(
                p["coverage"] == "complete"
                and any(
                    issues[i]["code"] == "BLANK_PAGE_ASSESSED" for i in p["issue_ids"]
                ),
                "unsubstantiated blank page",
            )
    require(
        len(order) == len(set(order)) == len(blocks) and set(order) == set(blocks),
        "orphan/duplicate block",
    )
    for oid, o in obs.items():
        require(
            o["page"] in pages and o["frame_id"] in frames,
            "unknown observation location",
        )
        pn, f = frames[o["frame_id"]]
        require(pn == o["page"], "cross-page frame")
        require(
            o["owner_block_id"] is None
            or o["owner_block_id"] in blocks
            and blocks[o["owner_block_id"]]["page"] == pn,
            "invalid ownership",
        )
        require(
            set(o["reason_issue_ids"]) <= issues.keys(), "unknown disposition issue"
        )
        if o["disposition"] != "emitted":
            require(bool(o["reason_issue_ids"]), "unexplained observation disposition")
        else:
            require(
                o["owner_block_id"] is not None, "emitted observation without owner"
            )
        if o["disposition"] == "restricted":
            require(
                any(
                    issues[i]["code"] == "VISIBILITY_CONFLICT"
                    and issues[i]["exposure"] == "restricted"
                    and issues[i].get("page") == pn
                    for i in pages[pn]["issue_ids"]
                ),
                "restricted observation lacks page restriction",
            )
        if o["disposition"] != "restricted":
            require(
                all(
                    0 <= x <= f["width"] and 0 <= y <= f["height"]
                    for x, y in o["polygon"]
                ),
                "unbounded observation",
            )
    for sid, s in spans.items():
        require(
            s["page"] in pages and set(s["observation_ids"]) <= obs.keys(),
            "unknown span reference",
        )
        p = pages[s["page"]]
        require(
            all(
                0 <= x <= p["width_mpt"] and 0 <= y <= p["height_mpt"]
                for x, y in s["polygon_mpt"]
            ),
            "unbounded span",
        )
        x0, y0, x1, y1 = s["bbox_mpt"]
        require(
            0 <= x0 <= x1 <= p["width_mpt"] and 0 <= y0 <= y1 <= p["height_mpt"],
            "invalid bbox",
        )
        require(
            all(x0 <= x <= x1 and y0 <= y <= y1 for x, y in s["polygon_mpt"]),
            "bbox does not contain polygon",
        )
        for oid in s["observation_ids"]:
            require(obs[oid]["page"] == s["page"], "cross-page span")
            polygon, _ = project(
                obs[oid]["polygon"], frames[obs[oid]["frame_id"]][1]["to_page_mpt"]
            )
            require(
                all(
                    x0 <= max(0, min(p["width_mpt"], x)) <= x1
                    and y0 <= max(0, min(p["height_mpt"], y)) <= y1
                    for x, y in polygon
                ),
                "span omits observation geometry",
            )
    for t in transforms.values():
        require(set(t["source_span_ids"]) <= spans.keys(), "unknown transform span")
        covered = {
            oid for sid in t["source_span_ids"] for oid in spans[sid]["observation_ids"]
        }
        require(
            all(entry["observation_id"] in covered for entry in t["input"]),
            "transform source omits input geometry",
        )
        before = []
        for entry in t["input"]:
            require(entry["observation_id"] in obs, "unknown transform observation")
            before.append(_range(entry["range"], obs[entry["observation_id"]]["text"]))
        require("".join(before) == t["before"], "transformation input mismatch")
        require(t["input"] or t["kind"] == "structural", "unattributed inference")
    used = {oid: set() for oid in obs}
    used_transforms = set()

    def view(v, bid, track=False):
        position = 0
        input_ranges = {}
        for m in v["map"]:
            start, end = m["output_range"]
            require(start == position and end >= start, "overlap/gap in output mapping")
            output = _range(m["output_range"], v["text"])
            oid, tid = m["observation_id"], m["transform_id"]
            if oid is not None:
                require(
                    oid in obs and obs[oid]["owner_block_id"] == bid,
                    "observation owned by another block",
                )
                require(m["observation_range"] is not None, "missing observation range")
                original = _range(m["observation_range"], obs[oid]["text"])
                covered_range = set(range(*m["observation_range"]))
                require(
                    not covered_range & input_ranges.setdefault(oid, set()),
                    "repeated observation text in one view",
                )
                input_ranges[oid].update(covered_range)
                if track:
                    used[oid].update(range(*m["observation_range"]))
            else:
                require(
                    m["observation_range"] is None and tid is not None,
                    "unattributed insertion",
                )
                original = ""
            if tid is not None:
                require(tid in transforms, "unknown transformation")
                t = transforms[tid]
                require(t["after"] == output, "transformation output mismatch")
                if oid is not None:
                    require(
                        {"observation_id": oid, "range": m["observation_range"]}
                        in t["input"],
                        "transformation does not bind mapped input",
                    )
                else:
                    require(
                        not t["input"] and t["kind"] == "structural",
                        "invalid insertion",
                    )
                used_transforms.add(tid)
            else:
                require(original == output, "unrecorded text change")
            position = end
        require(position == len(v["text"]), "unmapped output suffix")

    edges = {bid: [] for bid in blocks}
    cell_ids = set()
    for bid, b in blocks.items():
        require(
            b["source_span_ids"] and set(b["source_span_ids"]) <= spans.keys(),
            "missing block span",
        )
        require(
            all(spans[s]["page"] == b["page"] for s in b["source_span_ids"]),
            "cross-page block span",
        )
        covered = {
            oid for sid in b["source_span_ids"] for oid in spans[sid]["observation_ids"]
        }
        owned = {oid for oid, o in obs.items() if o["owner_block_id"] == bid}
        require(owned <= covered, "block citation omits owned observations")
        require(set(b["issue_ids"]) <= issues.keys(), "unknown block issue")
        for name in ("observed", "reading"):
            view(b[name], bid, True)
        if b["parent_id"] is not None:
            require(b["parent_id"] in blocks, "unknown parent")
            edges[bid].append(b["parent_id"])
        for r in b["relations"]:
            require(r["target_id"] in blocks, "unknown relation")
            if r["kind"] == "continues":
                edges[bid].append(r["target_id"])
        payload = b["payload"]
        if b["type"] == "unresolved_region":
            require(
                any(
                    issues[i]["code"] == payload["reason_code"] for i in b["issue_ids"]
                ),
                "unresolved region reason has no supporting issue",
            )
        if b["type"] == "equation":
            require(
                (payload["latex"] is not None)
                == (payload["representation"] == "reconstructed_latex"),
                "invalid equation representation",
            )
        if b["type"] == "figure":
            require(
                set(payload["caption_block_ids"]) <= blocks.keys(), "unknown caption"
            )
        if b["type"] == "table":
            occupied = set()
            for cell in payload["cells"]:
                require(cell["id"] not in cell_ids, "duplicate cell id")
                cell_ids.add(cell["id"])
                require(
                    cell["row"] + cell["row_span"] <= payload["rows"]
                    and cell["column"] + cell["column_span"] <= payload["columns"],
                    "cell outside table",
                )
                for row in range(cell["row"], cell["row"] + cell["row_span"]):
                    for col in range(
                        cell["column"], cell["column"] + cell["column_span"]
                    ):
                        require((row, col) not in occupied, "overlapping cells")
                        occupied.add((row, col))
                require(
                    set(cell["source_span_ids"]) <= set(b["source_span_ids"])
                    and set(cell["issue_ids"]) <= issues.keys(),
                    "invalid cell references",
                )
                for name in ("observed", "reading"):
                    view(cell[name], bid)
                if cell["value_state"] != "text":
                    require(
                        not cell["observed"]["text"] and not cell["reading"]["text"],
                        "nontext cell has text",
                    )
    from .structure_validation import validate_structure

    validate_structure(d, require)
    visited = set()

    def visit(node, active):
        require(node not in active, "cyclic structural relationship")
        if node in visited:
            return
        for dest in edges[node]:
            visit(dest, active | {node})
        visited.add(node)

    for bid in blocks:
        visit(bid, set())
    require(used_transforms == set(transforms), "orphan transformation")
    for oid, o in obs.items():
        if o["disposition"] == "emitted":
            require(used[oid] == set(range(len(o["text"]))), "lost observation text")
    targets = {
        "block": blocks,
        "span": spans,
        "observation": obs,
        "cell": cell_ids,
        "page": {str(p) for p in pages},
        "document": {d["extraction_id"]},
    }
    for issue in issues.values():
        require(
            set(issue["target_ids"]) <= set(targets[issue["scope"]]),
            "unknown issue target",
        )
        if "page" in issue:
            require(issue["page"] in pages, "issue outside requested pages")
        if issue["code"] == "VISIBILITY_CONFLICT":
            require(
                issue["exposure"] == "restricted" and issue["severity"] == "blocking",
                "visibility issue downgraded",
            )
    v = d["quality"]["verification"]
    require(
        v["numeric_eligible"]
        == v["reread_completed"] + v["reread_unavailable"] + v["reread_not_attempted"],
        "verification accounting mismatch",
    )
    require(
        v["reread_attempted"] == v["reread_completed"] + v["reread_unavailable"]
        and v["reread_disagreed"] <= v["reread_completed"],
        "invalid reread counts",
    )
    require(d["quality"] == summarize(d, v), "derived quality mismatch")


def validate_document(document):
    if not isinstance(document, dict) or document.get("schema_version") != "1.0.0":
        raise EvidenceError(
            "UNSUPPORTED_SCHEMA", "validate", "Unsupported evidence schema"
        )
    try:
        _validate(document)
    except EvidenceError:
        raise
    except Exception as exc:
        raise EvidenceError(
            "INVALID_RESULT", "validate", "Evidence integrity validation failed"
        ) from exc
