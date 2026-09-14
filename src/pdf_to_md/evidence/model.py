"""Public SDK options. Operational budgets are deliberately separate from recipes."""

from dataclasses import asdict, dataclass
from typing import Callable, Dict, Any

DocumentResult = Dict[str, Any]
InspectionResult = Dict[str, Any]
ProgressSink = Callable[[dict], None]
FEATURES = ("table_structure", "text", "visual_regions")


class EvidenceError(ValueError):
    def __init__(self, code, stage, safe_message, *, limit=None):
        super().__init__(safe_message)
        self.code, self.stage, self.safe_message = code, stage, safe_message
        self.limit = limit
        self.retryable = code == "STORAGE_UNAVAILABLE"


@dataclass(frozen=True)
class Limits:
    input_bytes: int = 20 * 1024**2
    pages: int = 500
    page_content_bytes: int = 10_000_000
    page_pixels: int = 40_000_000
    text_codepoints: int = 2_000_000
    blocks: int = 5_000
    observations: int = 500_000
    spans: int = 100_000
    issues: int = 25_000
    document_bytes: int = 128 * 1024**2
    artifact_bytes: int = 256 * 1024**2
    disk_bytes: int = 1024**3

    def __post_init__(self):
        for name, value in asdict(self).items():
            if type(value) is not int or not 0 < value <= 2**53 - 1:
                raise EvidenceError(
                    "INVALID_OPTIONS",
                    "options",
                    "Limits must be positive safe integers",
                )

    def check(self, name, value):
        if value > getattr(self, name):
            raise EvidenceError(
                "LIMIT_EXCEEDED",
                "extract",
                "Document exceeds processing limit",
                limit=name,
            )


@dataclass(frozen=True)
class Profile:
    profile_id: str = "evidence-conservative-1"
    requested_pages: tuple = ()
    required_features: tuple = FEATURES
    mode: str = "auto"
    asset_mode: str = "none"
    languages: tuple = ("eng",)
    dpi: int = 300
    psm: int = 3
    verify_numbers: bool = True
    max_rereads_per_page: int = 80
    max_rereads_per_document: int = 1000

    def __post_init__(self):
        from ..ocr import OcrOptions

        if self.asset_mode not in ("none", "figures", "regions"):
            raise EvidenceError("INVALID_OPTIONS", "options", "Invalid asset mode")

        if self.profile_id != "evidence-conservative-1" or self.mode not in (
            "auto",
            "digital_only",
            "ocr_all",
        ):
            raise EvidenceError(
                "INVALID_OPTIONS",
                "options",
                "Unsupported evidence profile or routing mode",
            )
        for values in (self.requested_pages, self.required_features, self.languages):
            if not isinstance(values, tuple) or len(set(values)) != len(values):
                raise EvidenceError(
                    "INVALID_OPTIONS",
                    "options",
                    "Profile lists must be immutable and unique",
                )
        if (
            any(type(p) is not int or p < 1 for p in self.requested_pages)
            or tuple(sorted(self.requested_pages)) != self.requested_pages
        ):
            raise EvidenceError(
                "INVALID_OPTIONS",
                "options",
                "Pages must be ascending positive integers",
            )
        if not self.required_features or not set(self.required_features) <= set(
            FEATURES
        ):
            raise EvidenceError("INVALID_OPTIONS", "options", "Unsupported feature set")
        if not self.languages:
            raise EvidenceError(
                "INVALID_OPTIONS", "options", "OCR languages are required"
            )
        try:
            OcrOptions(
                "+".join(self.languages), self.psm, self.dpi, self.verify_numbers
            )
        except (ValueError, TypeError):
            raise EvidenceError(
                "INVALID_OPTIONS", "options", "Invalid OCR options"
            ) from None
        for value in (self.max_rereads_per_page, self.max_rereads_per_document):
            if type(value) is not int or not 0 <= value <= 1000:
                raise EvidenceError(
                    "INVALID_OPTIONS", "options", "Invalid reread count limit"
                )
