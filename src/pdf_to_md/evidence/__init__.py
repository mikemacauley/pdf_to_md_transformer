"""DEE-1 evidence SDK; legacy Markdown APIs remain independently compatible."""

from .model import (
    DocumentResult,
    EvidenceError,
    InspectionResult,
    Limits,
    Profile,
    ProgressSink,
)
from .extract import extract_document, extract_package, inspect_document
from .assets import EvidencePackage
from .validate import validate_document
from .render import render_markdown
from .bundle import validate_bundle, write_bundle, read_bundle

__all__ = [
    "DocumentResult",
    "EvidencePackage",
    "extract_package",
    "EvidenceError",
    "InspectionResult",
    "Limits",
    "Profile",
    "ProgressSink",
    "extract_document",
    "inspect_document",
    "validate_document",
    "render_markdown",
    "validate_bundle",
    "write_bundle",
    "read_bundle",
]
