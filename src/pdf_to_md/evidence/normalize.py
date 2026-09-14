"""Literal views and explicitly attributed structural separators."""


def text_view(entries, observations, transforms, span_ids, *, words=False, offset=0):
    output = ""
    mapping = []
    previous = None
    for oid, geometry in entries:
        text = observations[oid]["text"]
        separator = previous is not None and (
            words
            or geometry["x0"] - previous["x1"]
            > max(1, (geometry["bottom"] - geometry["top"]) * 0.18)
        )
        if (
            separator
            and output
            and not output[-1].isspace()
            and text
            and not text[0].isspace()
        ):
            tid = f"t{offset+len(transforms)+1:08d}"
            transforms[tid] = dict(
                rule_id="line_separator",
                rule_version="1",
                kind="structural",
                input=[],
                before="",
                after=" ",
                reason_code="GEOMETRIC_WORD_GAP",
                source_span_ids=span_ids,
            )
            mapping.append(
                dict(
                    output_range=[len(output), len(output) + 1],
                    observation_id=None,
                    observation_range=None,
                    transform_id=tid,
                )
            )
            output += " "
        mapping.append(
            dict(
                output_range=[len(output), len(output) + len(text)],
                observation_id=oid,
                observation_range=[0, len(text)],
                transform_id=None,
            )
        )
        output += text
        previous = geometry
    return dict(text=output, map=mapping)


def join_views(views, separator, transforms, span_ids, *, offset=0, rule="line_join"):
    """Concatenate views by shifting offsets; separators have their own ledger."""
    output = ""
    mapping = []
    for index, view in enumerate(views):
        if index:
            tid = f"t{offset+len(transforms)+1:08d}"
            transforms[tid] = dict(
                rule_id=rule,
                rule_version="1",
                kind="structural",
                input=[],
                before="",
                after=separator,
                reason_code="STRUCTURAL_SEPARATOR",
                source_span_ids=list(span_ids),
            )
            mapping.append(
                dict(
                    output_range=[len(output), len(output) + len(separator)],
                    observation_id=None,
                    observation_range=None,
                    transform_id=tid,
                )
            )
            output += separator
        for entry in view["map"]:
            mapping.append(
                dict(
                    entry, output_range=[v + len(output) for v in entry["output_range"]]
                )
            )
        output += view["text"]
    return dict(text=output, map=mapping)
