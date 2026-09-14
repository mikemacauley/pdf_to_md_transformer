"""Structural cross-record checks; independent of geometry proposal code."""

from collections import defaultdict

from .coordinates import project


def validate_structure(d, require):
    blocks, obs, spans, issues = (
        d[k] for k in ("blocks", "observations", "spans", "issues")
    )
    owned = defaultdict(set)
    for oid, o in obs.items():
        if o["owner_block_id"] is not None:
            owned[o["owner_block_id"]].add(oid)
    pages = {p["number"]: p for p in d["pages"]}
    frames = {f["id"]: f for p in d["pages"] for f in p["frames"]}
    for tid, t in d["transforms"].items():
        if t["kind"] == "structural":
            require(
                not t["input"]
                and t["before"] == ""
                and t["after"] in (" ", "\n", "\t", "\n\n"),
                "nonstructural text disguised as separator",
            )
    for bid, b in blocks.items():
        for name in ("observed", "reading"):
            mapped = defaultdict(set)
            for entry in b[name]["map"]:
                if entry["observation_id"] is not None:
                    mapped[entry["observation_id"]].update(
                        range(*entry["observation_range"])
                    )
            for oid in owned[bid]:
                if obs[oid]["disposition"] in ("emitted", "restricted"):
                    require(
                        mapped[oid] == set(range(len(obs[oid]["text"]))),
                        "block view lost owned input",
                    )
        if b["parent_id"] is not None:
            require(blocks[b["parent_id"]]["page"] == b["page"], "cross-page parent")
        for relation in b["relations"]:
            target = blocks[relation["target_id"]]
            if relation["kind"] == "continues":
                require(target["page"] >= b["page"], "backward continuation")
                require(target["type"] == b["type"], "incompatible continuation")
            if relation["kind"] == "caption_of":
                require(
                    target["type"] == "figure" and target["page"] == b["page"],
                    "invalid caption destination",
                )
                require(
                    bid in target["payload"]["caption_block_ids"],
                    "one-sided caption relation",
                )
        if b["type"] == "heading":
            label = b["payload"]["literal_label"]
            require(
                label is None or label in b["observed"]["text"],
                "invented heading label",
            )
        if b["type"] == "list_item":
            require(
                b["observed"]["text"].lstrip().startswith(b["payload"]["marker"]),
                "invented list marker",
            )
        if b["type"] == "figure":
            for caption in b["payload"]["caption_block_ids"]:
                require(
                    any(
                        r["kind"] == "caption_of" and r["target_id"] == bid
                        for r in blocks[caption]["relations"]
                    ),
                    "one-sided figure caption",
                )
        if b["type"] != "table":
            continue
        p = b["payload"]
        cells = p["cells"]
        occupied = set()
        assigned = set()
        require(
            [(c["row"], c["column"]) for c in cells]
            == sorted((c["row"], c["column"]) for c in cells),
            "unstable cell order",
        )
        x_edges, y_edges = {}, {}
        for c in cells:
            occupied.update(
                (r, col)
                for r in range(c["row"], c["row"] + c["row_span"])
                for col in range(c["column"], c["column"] + c["column_span"])
            )
            spanobs = {
                oid
                for sid in c["source_span_ids"]
                for oid in spans[sid]["observation_ids"]
            }
            require(not assigned & spanobs, "observation assigned to multiple cells")
            assigned |= spanobs
            require(spanobs <= owned[bid], "cell contains foreign observations")
            require(
                c["region_span_id"] in c["source_span_ids"],
                "cell region missing source binding",
            )
            region = spans[c["region_span_id"]]
            require(not region["observation_ids"], "cell region is not a geometry span")
            cellbox = region["bbox_mpt"]
            for edges, start, length, low, high in (
                (x_edges, c["column"], c["column_span"], cellbox[0], cellbox[2]),
                (y_edges, c["row"], c["row_span"], cellbox[1], cellbox[3]),
            ):
                for index, coordinate in ((start, low), (start + length, high)):
                    require(
                        index not in edges or edges[index] == coordinate,
                        "inconsistent cell grid boundary",
                    )
                    edges[index] = coordinate
            if p["structure"] == "resolved":
                for oid in spanobs:
                    _, box = project(
                        obs[oid]["polygon"], frames[obs[oid]["frame_id"]]["to_page_mpt"]
                    )
                    require(
                        box[0] >= cellbox[0] - 500
                        and box[1] >= cellbox[1] - 500
                        and box[2] <= cellbox[2] + 500
                        and box[3] <= cellbox[3] + 500,
                        "resolved cell crosses source boundary",
                    )
            for view in ("observed", "reading"):
                mapped = {
                    m["observation_id"]
                    for m in c[view]["map"]
                    if m["observation_id"] is not None
                }
                require(mapped == spanobs, "cell text and source disagree")
            if c["value_state"] in ("unreadable", "absent"):
                require(
                    p["structure"] == "candidate" and bool(c["issue_ids"]),
                    "unresolved cell in resolved table",
                )
        for edges in (x_edges, y_edges):
            values = [edges[i] for i in sorted(edges)]
            require(values == sorted(set(values)), "nonmonotonic cell grid")
        require(len(occupied) == p["rows"] * p["columns"], "lost table coordinate/row")
        require(assigned == owned[bid], "table cell partition lost observations")
        if p["structure"] == "candidate":
            require(
                any(
                    issues[i]["code"] == "TABLE_STRUCTURE_UNCERTAIN"
                    for i in b["issue_ids"]
                ),
                "unflagged candidate table",
            )
            require(
                pages[b["page"]]["feature_status"]["table_structure"] == "unresolved",
                "candidate table cleared quality",
            )
        for name in ("observed", "reading"):
            expected = "\n".join(
                "\t".join(c[name]["text"] for c in cells if c["row"] == r)
                for r in range(p["rows"])
            )
            require(b[name]["text"] == expected, "table concatenation mismatch")

            def signatures(view, skip_grid=False):
                result = []
                for m in view["map"]:
                    tid = m["transform_id"]
                    if (
                        skip_grid
                        and tid is not None
                        and d["transforms"][tid]["rule_id"]
                        in ("table_row", "table_column")
                    ):
                        continue
                    result.append(
                        (
                            m["observation_id"],
                            m["observation_range"],
                            tid,
                            view["text"][slice(*m["output_range"])],
                        )
                    )
                return result

            require(
                signatures(b[name], True)
                == [s for c in cells for s in signatures(c[name])],
                "table mapping differs from cells",
            )

    from .assets import asset_scope
    import re

    scope = asset_scope(d)
    exports = d["recipe"]["exports"]
    require(len({e["kind"] for e in exports}) == len(exports), "duplicate export kinds")
    for b in blocks.values():
        name = b["payload"].get("asset_name")
        if name is not None:
            require(
                scope != "none" and re.fullmatch(r"crop_s[0-9]{8}\.png", name),
                "invalid asset reference",
            )
            sid = name[5:-4]
            require(sid in b["source_span_ids"], "asset does not bind block region")
        elif scope != "none" and b["type"] in (
            "figure",
            "equation",
            "unresolved_region",
        ):
            require(False, "required block asset absent")
