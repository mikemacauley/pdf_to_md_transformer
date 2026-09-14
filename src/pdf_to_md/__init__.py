"""PDF to MD Transformer -- deterministic, offline PDF to Markdown conversion."""

__version__ = "1.2.0"

from .engine import convert_file, convert_pdf_to_markdown  # noqa: F401

# Evidence uses a separate profile; importing this package keeps legacy callers
# independent of optional evidence initialization and native runtime inspection.
def __getattr__(name):
    if name in {"inspect_document", "extract_document", "validate_document",
                "render_markdown", "write_bundle", "Limits", "Profile", "extract_package", "read_bundle"}:
        from . import evidence
        return getattr(evidence, name)
    raise AttributeError(name)
