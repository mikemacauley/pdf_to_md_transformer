"""Geometry proposals and lossless page-local ownership.

Arbitration order is lattice tables, rule-bounded candidate tables, boxed
callouts, figures, equations, typographic callouts, and remaining text. Within
one family the stable tie-break is (top, left, bottom, right, parser sequence).
Detectors never supply the text of a block: only retained observations do.
"""

import copy
import re
from collections import Counter, defaultdict
from types import SimpleNamespace

from ..geometry import words_to_line_groups, make_line, order_items
from ..tables import find_h_rules_vector, group_rule_regions, _column_cuts
from ..sidebars import detect_boxed_regions, detect_typographic_blocks
from ..figures import detect_figure_regions, bind_caption
from ..engine import _split_equations
from .coordinates import millipoints, rectangle
from .normalize import text_view, join_views

FAMILIES = ("blocks", "spans", "observations", "transforms", "issues")
PREFIXES = dict(zip(FAMILIES, ("b", "s", "o", "t", "i")))
LIST = re.compile(
    r"^\s*((?:\d+(?:\.\d+)*[.)]|\([a-zA-Z0-9ivxlcdm]+\)|[a-zA-Z][.)]|[•▪‣◦*-]))\s+"
)
LABEL = re.compile(
    r"^\s*((?:(?:Article|Section|ARTICLE|SECTION)\s+)?\d+(?:\.\d+)*\.?)\s+"
)
PAGE_NUMBER = re.compile(
    r"^(?:Page\s+)?(?:\d+|[ivxlcdm]+)(?:\s+(?:of|/)\s+\d+)?$", re.I
)


def allocate(d, family, offsets):
    return f"{PREFIXES[family]}{offsets.get(family, 0)+len(d[family])+1:08d}"


def bbox(words):
    return [
        min(w["x0"] for w in words),
        min(w["top"] for w in words),
        max(w["x1"] for w in words),
        max(w["bottom"] for w in words),
    ]


def contains(box, word):
    x, y = (word["x0"] + word["x1"]) / 2, (word["top"] + word["bottom"]) / 2
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def overlaps(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def point_words(words):
    """Reuse point-based detector constants without changing observation units."""
    return [
        dict(
            w,
            **{k: w[k] / 1000 for k in ("x0", "x1", "top", "bottom")},
            size=w.get("size", w["bottom"] - w["top"]) / 1000,
        )
        for w in words
    ]


def groups(words, split=True):
    # Detector output preserves the original record through _source; no clean_text.
    points = [dict(w, _source=source) for w, source in zip(point_words(words), words)]
    return [
        [w["_source"] for w in group]
        for group in words_to_line_groups(points, split_columns=split)
    ]


class Builder:
    def __init__(
        self, d, result, limits, offsets, issue, method, conflict, reason, cell_ink=None
    ):
        self.d = d
        self.page = result
        self.limits = limits
        self.offsets = offsets
        self.issue = issue
        self.method = method
        self.conflict = conflict
        self.reason = reason
        self.cell_ink = cell_ink
        self.meta = {}
        self.claimed = set()

    def next(self, family):
        if family in ("blocks", "spans", "observations", "issues"):
            self.limits.check(
                family, self.offsets.get(family, 0) + len(self.d[family]) + 1
            )
        return allocate(self.d, family, self.offsets)

    def observe(self, w, owner, disposition=None, issues=None):
        oid = self.next("observations")
        self.d["observations"][oid] = dict(
            page=self.page["number"],
            method="ocr" if w["conf"] is not None else "digital",
            frame_id=w["frame"],
            polygon=w["polygon"],
            text=w["text"],
            confidence_bp=w["conf"],
            disposition=disposition or ("restricted" if self.conflict else "emitted"),
            owner_block_id=owner,
            reason_issue_ids=(
                issues
                if issues is not None
                else ([self.reason] if self.conflict else [])
            ),
        )
        return oid

    def span(self, box, oids=()):
        sid = self.next("spans")
        box = [
            max(0, min(self.page["width_mpt"], box[0])),
            max(0, min(self.page["height_mpt"], box[1])),
            max(0, min(self.page["width_mpt"], box[2])),
            max(0, min(self.page["height_mpt"], box[3])),
        ]
        self.d["spans"][sid] = dict(
            page=self.page["number"],
            polygon_mpt=rectangle(box),
            bbox_mpt=box,
            observation_ids=list(oids),
        )
        return sid

    def views(self, words, bid, region=None):
        views = []
        sids = []
        for group in groups(words):
            entries = [(self.observe(w, bid), w) for w in group]
            sid = self.span(bbox(group), [o for o, w in entries])
            sids.append(sid)
            views.append(
                text_view(
                    entries,
                    self.d["observations"],
                    self.d["transforms"],
                    [sid],
                    words=self.method == "ocr",
                    offset=self.offsets.get("transforms", 0),
                )
            )
        if not sids:
            sids = [self.span(region)]
        view = join_views(
            views,
            "\n",
            self.d["transforms"],
            sids,
            offset=self.offsets.get("transforms", 0),
            rule="line_join",
        )
        return view, sids

    def add(self, kind, words, region=None, payload=None, issue_ids=()):
        bid = self.next("blocks")
        view, sids = self.views(words, bid, region)
        if region is not None and words:
            sids.append(self.span(region))
        self.d["blocks"][bid] = dict(
            page=self.page["number"],
            type=kind,
            source_span_ids=sids,
            observed=view,
            reading=copy.deepcopy(view),
            issue_ids=list(dict.fromkeys([*self.page["issue_ids"], *issue_ids])),
            parent_id=None,
            relations=[],
            payload=payload or {},
        )
        self.page["block_order"].append(bid)
        box = region or bbox(words)
        sizes = Counter(
            w.get("size", w["bottom"] - w["top"]) for w in words for _ in w["text"]
        )
        self.meta[bid] = dict(
            box=list(box),
            size=sizes.most_common(1)[0][0] if sizes else 0,
            bold=bool(words)
            and all(
                "bold" in w.get("fontname", "").lower()
                for w in words
                if w["text"].strip()
            ),
            font=(
                Counter(w.get("fontname", "") for w in words).most_common(1)[0][0]
                if words
                else ""
            ),
        )
        return bid

    def gap(self, box, code, feature, *, severity="warning", rule="structure_1"):
        iid = self.issue(code, severity, rule=rule)
        self.page["feature_status"][feature] = "unresolved"
        if self.page["coverage"] == "complete":
            self.page["coverage"] = "partial"
        bid = self.add(
            "unresolved_region", [], box, dict(reason_code=code, asset_name=None), [iid]
        )
        # Exact source span binds the issue instead of declaring a whole-page gap.
        self.d["issues"][iid].update(
            scope="span", target_ids=self.d["blocks"][bid]["source_span_ids"]
        )
        return iid

    def table(self, words, box, cells, xs, ys, candidate=False):
        bid = self.next("blocks")
        payload = []
        all_sids = []
        taken = set()
        occupied = set()
        # Reserve block identity before constructing dependent cells and spans.
        self.d["blocks"][bid] = {}
        for cellbox in sorted(
            set(tuple(c) for c in cells), key=lambda b: (b[1], b[0], b[3], b[2])
        ):
            row, col = ys.index(cellbox[1]), xs.index(cellbox[0])
            rs, cs = ys.index(cellbox[3]) - row, xs.index(cellbox[2]) - col
            coordinates = {
                (r, c) for r in range(row, row + rs) for c in range(col, col + cs)
            }
            if not rs or not cs or coordinates & occupied:
                # Never discard text: reject geometric conflict and use candidate
                # atomic cells instead (the proposal is constructed before here).
                raise ValueError("overlapping table geometry")
            occupied |= coordinates
            inside = [
                w
                for i, w in enumerate(words)
                if i not in taken and contains(cellbox, w)
            ]
            taken.update(
                i
                for i, w in enumerate(words)
                if i not in taken and contains(cellbox, w)
            )
            view, sids = self.views(inside, bid, cellbox)
            sids.append(self.span(cellbox))
            all_sids.extend(sids)
            cell_issues = []
            state = "text" if view["text"] else "blank"
            if (
                state == "blank"
                and self.method == "ocr"
                and self.cell_ink is not None
                and self.cell_ink(cellbox)
            ):
                state = "unreadable"
                candidate = True
                iid = self.issue("UNREADABLE_REGION", "blocking", rule="cell_ink_1")
                self.d["issues"][iid].update(scope="span", target_ids=sids)
                cell_issues.append(iid)
                if "text" in self.d["scope"]["required_features"]:
                    self.page["feature_status"]["text"] = "unresolved"
            if any(
                w["x0"] < cellbox[0] - 500
                or w["x1"] > cellbox[2] + 500
                or w["top"] < cellbox[1] - 500
                or w["bottom"] > cellbox[3] + 500
                for w in inside
            ):
                candidate = True
            payload.append(
                dict(
                    id=f"{bid}_c{len(payload)+1:05d}",
                    row=row,
                    column=col,
                    row_span=rs,
                    column_span=cs,
                    header_role="unknown",
                    value_state=state,
                    observed=view,
                    reading=copy.deepcopy(view),
                    source_span_ids=sids,
                    region_span_id=sids[-1],
                    issue_ids=cell_issues,
                )
            )
        # Explicit absent coordinates preserve holes; they are never blank cells.
        for row in range(len(ys) - 1):
            for col in range(len(xs) - 1):
                if (row, col) in occupied:
                    continue
                candidate = True
                region = [xs[col], ys[row], xs[col + 1], ys[row + 1]]
                inside = [
                    w
                    for i, w in enumerate(words)
                    if i not in taken and contains(region, w)
                ]
                taken.update(
                    i
                    for i, w in enumerate(words)
                    if i not in taken and contains(region, w)
                )
                view, sids = self.views(inside, bid, region)
                sids.append(self.span(region))
                all_sids.extend(sids)
                payload.append(
                    dict(
                        id=f"{bid}_c{len(payload)+1:05d}",
                        row=row,
                        column=col,
                        row_span=1,
                        column_span=1,
                        header_role="unknown",
                        value_state="text" if view["text"] else "absent",
                        observed=view,
                        reading=copy.deepcopy(view),
                        source_span_ids=sids,
                        region_span_id=sids[-1],
                        issue_ids=[],
                    )
                )
        if len(taken) != len(words):
            raise ValueError("table proposal does not account for content")
        payload.sort(key=lambda c: (c["row"], c["column"]))
        issue_ids = list(self.page["issue_ids"])
        if candidate:
            iid = self.issue("TABLE_STRUCTURE_UNCERTAIN", rule="table_candidate_1")
            issue_ids.append(iid)
            self.page["feature_status"]["table_structure"] = "unresolved"
            if self.page["coverage"] == "complete":
                self.page["coverage"] = "partial"
            for cell in payload:
                cell["issue_ids"].append(iid)
        elif self.page["feature_status"]["table_structure"] != "unresolved":
            self.page["feature_status"]["table_structure"] = "resolved"
        # Header roles remain unknown unless a distinct bold first row supplies
        # typography evidence. No first-row promotion from table position alone.
        first = [w for w in words if w["top"] < ys[1] and w["text"].strip()]
        rest = [w for w in words if w["top"] >= ys[1] and w["text"].strip()]
        if (
            first
            and rest
            and all("bold" in w.get("fontname", "").lower() for w in first)
            and not all("bold" in w.get("fontname", "").lower() for w in rest)
        ):
            for cell in payload:
                cell["header_role"] = "column" if cell["row"] == 0 else "none"
        all_sids.append(self.span(box))
        rows = []
        for row in range(len(ys) - 1):
            rowcells = [c for c in payload if c["row"] == row]
            rows.append(
                join_views(
                    [c["observed"] for c in rowcells],
                    "\t",
                    self.d["transforms"],
                    all_sids,
                    offset=self.offsets.get("transforms", 0),
                    rule="table_column",
                )
            )
        view = join_views(
            rows,
            "\n",
            self.d["transforms"],
            all_sids,
            offset=self.offsets.get("transforms", 0),
            rule="table_row",
        )
        self.d["blocks"][bid] = dict(
            page=self.page["number"],
            type="table",
            source_span_ids=all_sids,
            observed=view,
            reading=copy.deepcopy(view),
            issue_ids=list(dict.fromkeys(issue_ids)),
            parent_id=None,
            relations=[],
            payload=dict(
                rows=len(ys) - 1,
                columns=len(xs) - 1,
                cells=payload,
                structure="candidate" if candidate else "resolved",
            ),
        )
        self.page["block_order"].append(bid)
        self.meta[bid] = dict(box=list(box), size=0, bold=False, font="")
        return bid


def build_page(
    d,
    result,
    page,
    words,
    alternatives,
    *,
    limits,
    offsets,
    issue,
    method,
    conflict,
    reason,
    ocr_rules=None,
    cell_ink=None,
):
    b = Builder(d, result, limits, offsets, issue, method, conflict, reason, cell_ink)
    left, top, _, _ = page.bbox

    def local(box):
        return [
            millipoints(box[0] - left),
            millipoints(box[1] - top),
            millipoints(box[2] - left),
            millipoints(box[3] - top),
        ]

    def native(box):
        return [
            box[0] / 1000 + left,
            box[1] / 1000 + top,
            box[2] / 1000 + left,
            box[3] / 1000 + top,
        ]

    remaining = list(words)
    claimed = []

    def take(box):
        nonlocal remaining
        inside = [w for w in remaining if contains(box, w)]
        remaining = [w for w in remaining if not contains(box, w)]
        return inside

    if not conflict and result["blank"] != "blank_by_rule":
        if "table_structure" in d["scope"]["required_features"]:
            tables = page.find_tables() if method == "digital" else []
            for table in sorted(
                tables, key=lambda t: (t.bbox[1], t.bbox[0], t.bbox[3], t.bbox[2])
            ):
                cells = [local(c) for c in table.cells]
                xs = sorted({c[i] for c in cells for i in (0, 2)})
                ys = sorted({c[i] for c in cells for i in (1, 3)})
                if len(xs) < 3 or len(ys) < 3:
                    continue  # a single box is a callout
                box = local(table.bbox)
                if any(overlaps(box, c) for c in claimed):
                    continue
                b.table(take(box), box, cells, xs, ys)
                claimed.append(box)
            # OCR rules remain in rendered-page coordinates; digital rules are
            # reused before their old string reconstruction stage.
            h, v = ocr_rules or (find_h_rules_vector(page), [])
            if method == "ocr":
                h = [
                    dict(
                        x0=r["x0"] / 1000 + left,
                        x1=r["x1"] / 1000 + left,
                        top=r["top"] / 1000 + top,
                    )
                    for r in h
                ]
                v = [
                    dict(
                        x=r["x"] / 1000 + left,
                        top=r["top"] / 1000 + top,
                        bottom=r["bottom"] / 1000 + top,
                    )
                    for r in v
                ]
            for region in group_rule_regions(
                h, skip_bboxes=[native(c) for c in claimed]
            ):
                box = local(region)
                if any(overlaps(box, c) for c in claimed):
                    continue
                inside = [w for w in remaining if contains(box, w)]
                if not inside:
                    continue
                points = point_words(inside)
                gs = words_to_line_groups(points)
                cuts = _column_cuts(gs)
                vertical = sorted(
                    {
                        millipoints(r["x"] - left)
                        for r in v
                        if region[0] - 2 <= r["x"] <= region[2] + 2
                        and r["top"] <= region[1] + 3
                        and r["bottom"] >= region[3] - 3
                    }
                )
                if len(vertical) >= 3:
                    xs = vertical
                    ys = sorted(
                        {
                            millipoints(r["top"] - top)
                            for r in h
                            if region[1] - 1 <= r["top"] <= region[3] + 1
                        }
                    )
                    # Detected exterior rule centers can be slightly inside words.
                    xs[0] = min(xs[0], box[0])
                    xs[-1] = max(xs[-1], box[2])
                    box = [xs[0], ys[0], xs[-1], ys[-1]]
                    cells = [
                        [x0, y0, x1, y1]
                        for y0, y1 in zip(ys, ys[1:])
                        for x0, x1 in zip(xs, xs[1:])
                    ]
                    b.table(take(box), box, cells, xs, ys, candidate=True)
                    claimed.append(box)
                elif cuts and len(gs) >= 2:
                    xs = [box[0], *[millipoints(c) for c in cuts], box[2]]
                    mid = [
                        round(
                            (max(w["bottom"] for w in a) + min(w["top"] for w in z))
                            * 500
                        )
                        for a, z in zip(gs, gs[1:])
                    ]
                    ys = [box[1], *mid, box[3]]
                    if len(set(xs)) != len(xs) or ys != sorted(set(ys)):
                        continue
                    cells = [
                        [x0, y0, x1, y1]
                        for y0, y1 in zip(ys, ys[1:])
                        for x0, x1 in zip(xs, xs[1:])
                    ]
                    b.table(take(box), box, cells, xs, ys, candidate=True)
                    claimed.append(box)
        if method == "digital":
            for region in detect_boxed_regions(page):
                box = local(region)
                intersecting = [c for c in claimed if overlaps(box, c)]
                # A larger panel may contain a table, but an intersecting or
                # identical box cannot reclaim its observations.
                if any(
                    not (
                        box[0] <= c[0]
                        and box[1] <= c[1]
                        and box[2] >= c[2]
                        and box[3] >= c[3]
                        and box != c
                    )
                    for c in intersecting
                ):
                    continue
                inside = [w for w in remaining if contains(box, w)]
                if inside:
                    children = [
                        bid for bid, m in b.meta.items() if m["box"] in intersecting
                    ]
                    parent = b.add("callout", take(box), box)
                    for child in children:
                        d["blocks"][child]["parent_id"] = parent
                    claimed.append(box)
        if "visual_regions" in d["scope"]["required_features"]:
            for region in detect_figure_regions(page, [native(c) for c in claimed]):
                box = local(region)
                if any(overlaps(box, c) for c in claimed):
                    continue
                if not any(e["kind"] == "png" for e in d["recipe"]["exports"]):
                    issue("ASSET_UNAVAILABLE", rule="optional_figure_asset_1")
                b.add(
                    "figure",
                    take(box),
                    box,
                    dict(asset_name=None, caption_block_ids=[]),
                )
                claimed.append(box)
                if result["feature_status"]["visual_regions"] != "unresolved":
                    result["feature_status"]["visual_regions"] = "resolved"
            for primitive in [*page.images, *page.curves, *page.lines, *page.rects]:
                region = local(
                    (
                        primitive["x0"],
                        primitive["top"],
                        primitive["x1"],
                        primitive["bottom"],
                    )
                )
                area = (region[2] - region[0]) * (region[3] - region[1])
                if (
                    method == "ocr"
                    and primitive in page.images
                    and area >= result["width_mpt"] * result["height_mpt"] * 0.92
                ):
                    continue  # the raster backing the OCR page, not a separate illustration
                if any(
                    c[0] - 500 <= region[0]
                    and c[1] - 500 <= region[1]
                    and c[2] + 500 >= region[2]
                    and c[3] + 500 >= region[3]
                    for c in claimed
                ):
                    continue
                region = [
                    min(region[0], result["width_mpt"] - 1000),
                    min(region[1], result["height_mpt"] - 1000),
                    min(result["width_mpt"], max(region[2], region[0] + 1000)),
                    min(result["height_mpt"], max(region[3], region[1] + 1000)),
                ]
                b.gap(
                    region,
                    "UNSUPPORTED_FEATURE",
                    "visual_regions",
                    severity="blocking",
                    rule="unclassified_visual_1",
                )
                claimed.append(region)
            for annotation in page.annots:
                box = local(
                    (
                        annotation["x0"],
                        annotation["top"],
                        annotation["x1"],
                        annotation["bottom"],
                    )
                )
                b.gap(
                    box,
                    "UNSUPPORTED_FEATURE",
                    "visual_regions",
                    severity="blocking",
                    rule="annotation_1",
                )
    linegroups = groups(remaining)
    lines = [make_line(point_words(g)) for g in linegroups]
    if not conflict and method == "digital":
        # The equation detector returns geometry; its cleaned/verbatim strings
        # are never substituted for observations, even when LaTeX is inferred.
        _, equations = _split_equations(
            page,
            [
                SimpleNamespace(
                    **dict(
                        vars(line),
                        x0=line.x0 + left,
                        x1=line.x1 + left,
                        top=line.top + top,
                        bottom=line.bottom + top,
                    )
                )
                for line in lines
            ],
            exclude_bboxes=[native(c) for c in claimed],
        )
        for eq in equations:
            box = [
                millipoints(eq.x0 - left),
                millipoints(eq.top - top),
                millipoints(eq.x1 - left),
                millipoints(eq.bottom - top),
            ]
            if any(overlaps(box, c) for c in claimed):
                continue
            content = take(box)
            if not content:
                continue
            iid = issue(
                "NORMALIZATION_INFERRED" if eq.latex else "EQUATION_UNINTERPRETED",
                "info" if eq.latex else "warning",
                rule="equation_geometry_1",
            )
            b.add(
                "equation",
                content,
                box,
                dict(
                    representation=(
                        "reconstructed_latex" if eq.latex else "uninterpreted_glyphs"
                    ),
                    latex=eq.latex,
                    asset_name=None,
                ),
                [iid],
            )
            claimed.append(box)
    # Rebuild after equations claimed their own observations.
    linegroups = groups(remaining)
    lines = [make_line(point_words(g)) for g in linegroups]
    body = Counter(
        round(line.size * 1000) for line in lines for _ in line.text
    ).most_common(1)
    if not conflict and body:
        for start, end in detect_typographic_blocks(lines, body[0][0] / 1000):
            content = [w for group in linegroups[start:end] for w in group]
            region = bbox(content)
            if not any(overlaps(region, c) for c in claimed):
                b.add("callout", take(region), region)
                claimed.append(region)
    for group in groups(remaining):
        b.add("paragraph", group)
    for word, rid in alternatives:
        b.observe(word, None, "alternative", [rid])
    # Gaps caused by OCR absence belong to embedded image regions, or the page
    # if explicitly requesting OCR for all text. Do not mark a table unreadable.
    for iid in list(result["issue_ids"]):
        code = d["issues"][iid]["code"]
        if code not in (
            "OCR_UNAVAILABLE",
            "OCR_LANGUAGE_UNAVAILABLE",
            "UNREADABLE_REGION",
            "VISIBILITY_CONFLICT",
        ):
            continue
        regions = [
            local((im["x0"], im["top"], im["x1"], im["bottom"])) for im in page.images
        ] or [[0, 0, result["width_mpt"], result["height_mpt"]]]
        for region in regions:
            b.add(
                "unresolved_region",
                [],
                region,
                dict(reason_code=code, asset_name=None),
                [iid],
            )
        if "text" in d["scope"]["required_features"]:
            result["feature_status"]["text"] = "unresolved"
    # Restore column-aware order with geometry helpers, preserving IDs as opaque
    # handles. Stable allocation order does not depend on later classifications.
    positioned = [
        SimpleNamespace(
            id=bid,
            x0=m["box"][0] / 1000,
            top=m["box"][1] / 1000,
            x1=m["box"][2] / 1000,
            bottom=m["box"][3] / 1000,
        )
        for bid, m in b.meta.items()
    ]
    if positioned:
        result["block_order"] = [
            item.id for item in order_items(positioned, 0, result["width_mpt"] / 1000)
        ]
    if len(result["block_order"]) != len(b.meta) or set(result["block_order"]) != set(
        b.meta
    ):
        result["block_order"] = sorted(
            b.meta, key=lambda bid: (b.meta[bid]["box"][1], b.meta[bid]["box"][0], bid)
        )
        iid = issue("READING_ORDER_UNCERTAIN", rule="order_fallback_1")
        for bid in result["block_order"]:
            d["blocks"][bid]["issue_ids"].append(iid)
        if "text" in d["scope"]["required_features"]:
            result["feature_status"]["text"] = "unresolved"
        if result["coverage"] == "complete":
            result["coverage"] = "partial"
    # Bind nearest caption once; the caption remains a distinct owned block.
    captions = []
    for bid in result["block_order"]:
        block = d["blocks"][bid]
        m = b.meta[bid]
        if block["type"] == "paragraph":
            captions.append(
                SimpleNamespace(
                    id=bid,
                    text=block["observed"]["text"],
                    x0=m["box"][0] / 1000,
                    top=m["box"][1] / 1000,
                    x1=m["box"][2] / 1000,
                    bottom=m["box"][3] / 1000,
                )
            )
    usedcaptions = set()
    for bid in result["block_order"]:
        if d["blocks"][bid]["type"] != "figure":
            continue
        available = [c for c in captions if c.id not in usedcaptions]
        index = bind_caption([v / 1000 for v in b.meta[bid]["box"]], available)
        if index is not None:
            caption = available[index].id
            usedcaptions.add(caption)
            d["blocks"][bid]["payload"]["caption_block_ids"].append(caption)
            d["blocks"][caption]["relations"].append(
                dict(kind="caption_of", target_id=bid, certainty="rule_supported")
            )
    return b.meta


def finalize_document(d, metadata):
    """Second-pass typography/furniture and conservative continuation relations."""
    frequencies = defaultdict(set)
    sizes = Counter()
    for page in d["pages"]:
        for bid in page["block_order"]:
            block = d["blocks"][bid]
            m = metadata[bid]
            if block["type"] != "paragraph":
                continue
            text = block["observed"]["text"].strip()
            if (
                m["box"][1] < page["height_mpt"] * 0.1
                or m["box"][3] > page["height_mpt"] * 0.9
            ):
                frequencies[(text, round(m["box"][0] / 5000))].add(page["number"])
            else:
                sizes[m["size"]] += len(text)
    body = sizes.most_common(1)[0][0] if sizes else 12000
    heading_sizes = sorted(
        {
            m["size"]
            for bid, m in metadata.items()
            if d["blocks"][bid]["type"] == "paragraph" and m["size"] > body * 1.12
        },
        reverse=True,
    )
    previous = None
    for page in d["pages"]:
        list_indents = []
        heading_stack = []
        for bid in page["block_order"]:
            block = d["blocks"][bid]
            m = metadata[bid]
            if block["type"] != "paragraph":
                continue
            text = block["observed"]["text"].strip()
            if any(r["kind"] == "caption_of" for r in block["relations"]):
                continue
            margin = (
                m["box"][1] < page["height_mpt"] * 0.1
                or m["box"][3] > page["height_mpt"] * 0.9
            )
            if margin and (
                PAGE_NUMBER.fullmatch(text)
                or (
                    m["size"] <= body * 1.05
                    and len(frequencies[(text, round(m["box"][0] / 5000))]) >= 2
                )
            ):
                role = (
                    "page_number"
                    if PAGE_NUMBER.fullmatch(text)
                    else "header" if m["box"][1] < page["height_mpt"] / 2 else "footer"
                )
                block.update(type="page_furniture", payload=dict(role=role))
                if role == "page_number":
                    page["printed_label"] = text
                continue
            if any(r["kind"] == "caption_of" for r in block["relations"]):
                continue
            marker = LIST.match(text)
            label = LABEL.match(text)
            # OCR token boxes measure ink, not font size: they cannot establish
            # typographic heading levels. Preserve those lines without inference.
            isheading = page["method"] == "digital" and (
                m["size"] > body * 1.12
                or (m["bold"] and len(text) < 160 and not marker)
            )
            if isheading:
                level = (
                    min(6, heading_sizes.index(m["size"]) + 1)
                    if m["size"] in heading_sizes
                    else min(6, len(heading_sizes) + 1)
                )
                block.update(
                    type="heading",
                    payload=dict(
                        level=level,
                        literal_label=label.group(1) if label else None,
                        basis="typography",
                    ),
                )
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                block["parent_id"] = heading_stack[-1][1] if heading_stack else None
                heading_stack.append((level, bid))
            elif marker:
                x = m["box"][0]
                while list_indents and x < list_indents[-1] - 2000:
                    list_indents.pop()
                if not list_indents or x > list_indents[-1] + 2000:
                    list_indents.append(x)
                block.update(
                    type="list_item",
                    payload=dict(marker=marker.group(1), depth=len(list_indents) - 1),
                )
                block["parent_id"] = heading_stack[-1][1] if heading_stack else None
            elif heading_stack:
                block["parent_id"] = heading_stack[-1][1]
        # Link page-local boundary tables only, preserving both independently.
        tables = [
            bid for bid in page["block_order"] if d["blocks"][bid]["type"] == "table"
        ]
        if previous and page["number"] == previous["number"] + 1 and tables:
            earlier = [
                bid
                for bid in previous["block_order"]
                if d["blocks"][bid]["type"] == "table"
            ]
            if earlier:
                a, z = earlier[-1], tables[0]
                if (
                    d["blocks"][a]["payload"]["columns"]
                    == d["blocks"][z]["payload"]["columns"]
                    and metadata[a]["box"][3] > previous["height_mpt"] * 0.7
                    and metadata[z]["box"][1] < page["height_mpt"] * 0.3
                ):
                    d["blocks"][a]["relations"].append(
                        dict(kind="continues", target_id=z, certainty="candidate")
                    )
        previous = page


def merge_paragraphs(d, metadata):
    """Keep observed line breaks, reflow reading spaces, never repair hyphens."""
    for page in d["pages"]:
        result = []
        for bid in page["block_order"]:
            block = d["blocks"][bid]
            if result:
                prior = d["blocks"][result[-1]]
                a = metadata[result[-1]]
                z = metadata[bid]
                join = (
                    block["type"] == "paragraph"
                    and prior["type"] in ("paragraph", "list_item")
                    and not block["relations"]
                    and not prior["relations"]
                    and block["parent_id"] == prior["parent_id"]
                    and abs(a["size"] - z["size"]) <= 500
                    and 0 <= z["box"][1] - a["box"][3] <= max(1000, a["size"] * 0.8)
                    and (
                        abs(z["box"][0] - a["box"][0]) <= 2000
                        or prior["type"] == "list_item"
                        and a["box"][0] < z["box"][0] < a["box"][0] + 40000
                    )
                )
                if join:
                    sids = prior["source_span_ids"] + block["source_span_ids"]
                    for view, separator in (("observed", "\n"), ("reading", " ")):
                        prior[view] = join_views(
                            [prior[view], block[view]],
                            separator,
                            d["transforms"],
                            sids,
                            rule="paragraph_join",
                        )
                    prior["source_span_ids"] = sids
                    prior["issue_ids"] = list(
                        dict.fromkeys(prior["issue_ids"] + block["issue_ids"])
                    )
                    for o in d["observations"].values():
                        if o["owner_block_id"] == bid:
                            o["owner_block_id"] = result[-1]
                    a["box"] = [
                        min(a["box"][0], z["box"][0]),
                        a["box"][1],
                        max(a["box"][2], z["box"][2]),
                        z["box"][3],
                    ]
                    sid = f's{max(int(k[1:]) for k in d["spans"])+1:08d}'
                    d["spans"][sid] = dict(
                        page=page["number"],
                        polygon_mpt=rectangle(a["box"]),
                        bbox_mpt=list(a["box"]),
                        observation_ids=[],
                    )
                    prior["source_span_ids"].append(sid)
                    del d["blocks"][bid]
                    del metadata[bid]
                    continue
            result.append(bid)
        page["block_order"] = result
