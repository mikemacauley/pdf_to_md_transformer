"""Pure ordinary projections. Source strings never become Markdown syntax."""

import html
import re


def withheld_pages(document):
    return {
        p["number"]
        for p in document["pages"]
        if any(
            document["issues"][i]["code"] == "VISIBILITY_CONFLICT"
            or document["issues"][i]["exposure"] == "restricted"
            for i in p["issue_ids"]
        )
    }


def escape_text(text):
    text = html.escape(text, quote=False)
    # Escape all Markdown punctuation, including link/autolink and image syntax.
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~\-])", r"\\\1", text)


def ordinary_issues(document):
    return {
        iid: (
            {
                k: i[k]
                for k in ("code", "severity", "page", "suggested_action")
                if k in i
            }
            if i["exposure"] == "restricted"
            else i
        )
        for iid, i in document["issues"].items()
    }


def render_markdown(document, *, view="reading"):
    from .validate import validate_document

    validate_document(document)
    if view not in ("reading", "observed"):
        raise ValueError("Unsupported machine text view")
    withheld = withheld_pages(document)
    lines = [
        "# Document evidence",
        "",
        "Coverage: " + document["quality"]["coverage"] + ".",
        "",
    ]
    for page in document["pages"]:
        lines += ["## Page " + str(page["number"]), ""]
        if page["number"] in withheld:
            lines += ["[Page withheld; authorized source review required.]", ""]
            continue
        if page["blank"] == "blank_by_rule":
            lines += ["[Blank page assessed by deterministic rule.]", ""]
        for iid in page["issue_ids"]:
            issue = document["issues"][iid]
            lines += ["Issue: " + issue["code"] + " (" + issue["severity"] + ").", ""]
        for bid in page["block_order"]:
            block = document["blocks"][bid]
            if block["type"] == "unresolved_region":
                lines += ["[Unresolved source region; review original.]", ""]
                continue
            text = block[view]["text"]
            kind = block["type"]
            if kind == "table":
                payload = block["payload"]
                if payload["structure"] == "candidate":
                    lines += ["[Candidate table structure; review original.]", ""]
                # Labeled rows preserve headerless/merged/absent semantics without
                # promoting a first row to a Markdown header.
                for row in range(payload["rows"]):
                    lines += ["Row " + str(row + 1) + ":", ""]
                    for cell in payload["cells"]:
                        if cell["row"] != row:
                            continue
                        label = "Column " + str(cell["column"] + 1)
                        if cell["row_span"] > 1 or cell["column_span"] > 1:
                            label += f" (row span {cell['row_span']}, column span {cell['column_span']})"
                        value = (
                            escape_text(cell[view]["text"])
                            if cell["value_state"] == "text"
                            else "[" + cell["value_state"] + "]"
                        )
                        lines += [label + ": " + value, ""]
                continue
            if kind == "heading":
                lines += [
                    "#" * (block["payload"]["level"] or 1) + " " + escape_text(text),
                    "",
                ]
            elif kind == "list_item":
                lines += [
                    "  " * (block["payload"]["depth"] or 0) + "- " + escape_text(text),
                    "",
                ]
            elif kind == "callout":
                lines += [
                    "\n".join("> " + escape_text(line) for line in text.split("\n")),
                    "",
                ]
            elif kind == "equation":
                representation = block["payload"]["representation"]
                lines += [
                    "[Equation: " + representation + "; compare with the source.]",
                    "",
                ]
                # Literal glyphs are never promoted to executable math or HTML.
                lines += [escape_text(text), ""]
                if representation == "reconstructed_latex":
                    lines += [
                        "Reconstructed LaTeX (inferred from glyph geometry):",
                        "",
                        escape_text(block["payload"]["latex"]),
                        "",
                    ]
            elif kind == "page_furniture":
                lines += [
                    "[Page " + block["payload"]["role"] + "] " + escape_text(text),
                    "",
                ]
            else:
                if kind == "figure":
                    lines += ["[Figure; source region retained.]", ""]
                lines += [escape_text(text), ""]
            name = block["payload"].get("asset_name")
            if name is not None:
                lines += ["![Source region](" + name + ")", ""]
    return "\n".join(lines)
