"""Pure immutable machine citation checks; source authority stays with the host."""

from .canonical import canonical, digest
from .model import EvidenceError
from .render import withheld_pages
from .validate import validate_document


def resolve_citation(document, citation, *, authorize, quote=None):
    # Do not expose existence, hashes, ranges or validation errors before access.
    if not authorize():
        raise EvidenceError("SOURCE_UNAVAILABLE", "citation", "Source unavailable")
    validate_document(document)
    try:
        expected = {
            "source_hash",
            "extraction_id",
            "document_hash",
            "block_id",
            "view",
            "range",
            "view_hash",
        }
        if set(citation) != expected or citation["view"] not in ("observed", "reading"):
            raise ValueError("unsupported citation")
        if (
            citation["source_hash"] != document["source"]["sha256"]
            or citation["extraction_id"] != document["extraction_id"]
            or citation["document_hash"] != digest(canonical(document))
        ):
            raise ValueError("revision mismatch")
        block = document["blocks"][citation["block_id"]]
        if block["page"] in withheld_pages(document):
            raise EvidenceError(
                "SOURCE_UNAVAILABLE", "citation", "Source review required"
            )
        text = block[citation["view"]]["text"]
        if digest(text.encode()) != citation["view_hash"]:
            raise ValueError("view mismatch")
        span = citation["range"]
        if (
            type(span) is not list
            or len(span) != 2
            or any(type(x) is not int for x in span)
        ):
            raise ValueError("invalid range")
        start, end = span
        if not 0 <= start <= end <= len(text):
            raise ValueError("range outside view")
        literal = text[start:end]
        if quote is not None and quote != literal:
            raise ValueError("quote mismatch")
        return literal
    except EvidenceError:
        raise
    except Exception as exc:
        raise EvidenceError(
            "INVALID_RESULT", "citation", "Citation does not match immutable evidence"
        ) from exc
