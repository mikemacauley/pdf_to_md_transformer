"""Derived coverage is independent of successful execution or business approval."""

from collections import Counter

COUNTERS = (
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


def summarize(document, verification=None):
    pages = document["pages"]
    groups = {
        kind: [p["number"] for p in pages if p["coverage"] == kind]
        for kind in ("complete", "partial", "unreadable")
    }
    blank = [p["number"] for p in pages if p["blank"] == "blank_by_rule"]
    coverage = "complete" if len(groups["complete"]) == len(pages) else "partial"
    usable = any(
        b["observed"]["text"]
        and not any(
            document["issues"][i]["code"] == "VISIBILITY_CONFLICT"
            for i in p["issue_ids"]
        )
        for p in pages
        for bid in p["block_order"]
        for b in [document["blocks"][bid]]
    )
    if coverage != "complete" and not usable and len(blank) != len(pages):
        coverage = "unreadable"
    by_code = Counter(i["code"] for i in document["issues"].values())
    by_severity = Counter(i["severity"] for i in document["issues"].values())
    return dict(
        coverage=coverage,
        requested_pages=[p["number"] for p in pages],
        complete_pages=groups["complete"],
        partial_pages=groups["partial"],
        unreadable_pages=groups["unreadable"],
        blank_pages=blank,
        issue_counts_by_code=dict(sorted(by_code.items())),
        issue_counts_by_severity={
            k: by_severity[k] for k in ("info", "warning", "blocking")
        },
        unresolved_region_count=sum(
            b["type"] == "unresolved_region" for b in document["blocks"].values()
        ),
        verification=verification or dict.fromkeys(COUNTERS, 0),
    )
