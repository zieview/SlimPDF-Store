"""Evidence contracts for engineering-drawing source analysis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class PdfSourceKind(str, Enum):
    """Dominant source representation of a PDF page or document."""

    VECTOR = "vector"
    SCANNED = "scanned"
    HYBRID = "hybrid"


class TextEvidenceSource(str, Enum):
    """Provenance of text evidence; native PDF text outranks OCR evidence."""

    NATIVE_PDF = "native_pdf"
    OCR = "ocr"


class EngineeringReviewStatus(str, Enum):
    """Decision state for a normalization suggestion."""

    PASS = "pass"
    REVIEW = "review"
    AUTO_CORRECT = "auto_correct"
    APPROVED = "approved"


class EngineeringFeedbackAction(str, Enum):
    """Explicit user decision stored in a review draft."""

    ACCEPT_RAW = "accept_raw"
    CORRECT = "correct"


class EngineeringTokenKind(str, Enum):
    """Conservative engineering-token categories used for review."""

    TAG = "tag"
    VALUE = "value"
    DICTIONARY = "dictionary"


class EngineeringValueKind(str, Enum):
    """Conservatively recognized engineering value categories."""

    PIPE_SIZE = "pipe_size"
    DIAMETER = "diameter"
    PRESSURE = "pressure"
    TEMPERATURE = "temperature"
    REFRIGERANT = "refrigerant"
    DIMENSION = "dimension"
    VOLTAGE = "voltage"


class GeometryEvidenceSource(str, Enum):
    """Origin of geometry kept independent from OCR text recognition."""

    VECTOR_PDF = "vector_pdf"
    RASTER_DETECTOR = "raster_detector"


class ExternalListKind(str, Enum):
    """Approved engineering reference-list categories."""

    BOM = "bom"
    EQUIPMENT = "equipment"
    VALVE = "valve"
    LINE = "line"


class CrossSheetIssueKind(str, Enum):
    """Non-destructive external validation issue categories."""

    MISSING_FROM_REFERENCE = "missing_from_reference"
    MISSING_FROM_DRAWING = "missing_from_drawing"
    DUPLICATE_REFERENCE = "duplicate_reference"
    CONFLICTING_REFERENCE = "conflicting_reference"


class NumericalConsistencyIssueKind(str, Enum):
    """Review-only engineering value conflict categories."""

    CONFLICTING_VALUE = "conflicting_value"


@dataclass(frozen=True, slots=True)
class TextEvidence:
    """One traceable text span in drawing coordinates."""

    text: str
    bounding_box: tuple[float, float, float, float]
    source: TextEvidenceSource
    confidence: float
    font_name: str | None = None
    font_size: float | None = None
    rotation: int = 0


@dataclass(frozen=True, slots=True)
class LineSegment:
    """One source-coordinate line segment."""

    start: tuple[float, float]
    end: tuple[float, float]
    source: GeometryEvidenceSource


@dataclass(frozen=True, slots=True)
class SymbolEvidence:
    """One independently detected symbol with explicit provenance."""

    symbol_id: str
    kind: str
    bounding_box: tuple[float, float, float, float]
    source: GeometryEvidenceSource
    confidence: float


@dataclass(frozen=True, slots=True)
class TextSymbolRelationship:
    """Scored, reviewable relationship between text and a drawing symbol."""

    text_index: int
    symbol_id: str
    score: float
    distance_score: float
    connectivity_score: float
    alignment_score: float
    semantic_score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExternalTagRecord:
    """One reference-list row with exact source provenance."""

    tag: str
    list_kind: ExternalListKind
    source_path: str
    sheet_name: str
    row_number: int
    attributes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class EngineeringReferenceSelection:
    """User-selected external reference list and optional explicit TAG column."""

    path: Path
    list_kind: ExternalListKind
    tag_column: str | None = None


@dataclass(frozen=True, slots=True)
class EngineeringFeedbackDecision:
    """One user-entered decision; it never mutates analysis evidence."""

    text_id: str
    action: EngineeringFeedbackAction
    corrected_text: str


@dataclass(frozen=True, slots=True)
class EngineeringValueEvidence:
    """One typed value extracted without changing its source characters."""

    raw_text: str
    normalized_text: str
    kind: EngineeringValueKind
    value_text: str
    unit: str | None
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class EngineeringTextValue:
    """A typed value linked to its source text evidence index."""

    text_index: int
    evidence: EngineeringValueEvidence


@dataclass(frozen=True, slots=True)
class EngineeringValueObservation:
    """One typed value explicitly associated with an engineering entity."""

    entity_key: str
    kind: EngineeringValueKind
    value_text: str
    unit: str | None
    provenance: str
    confidence: float


@dataclass(frozen=True, slots=True)
class NumericalConsistencyIssue:
    """Conflicting explicit observations that require human review."""

    kind: NumericalConsistencyIssueKind
    entity_key: str
    value_kind: EngineeringValueKind
    unit: str | None
    observed_values: tuple[str, ...]
    provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CrossSheetValidationIssue:
    """One review-only inconsistency between drawing evidence and reference lists."""

    kind: CrossSheetIssueKind
    tag: str
    message: str
    provenance: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CrossSheetValidationReport:
    """External validation result; it never changes OCR or source documents."""

    observed_tags: tuple[str, ...]
    reference_count: int
    issues: tuple[CrossSheetValidationIssue, ...]

    @property
    def requires_review(self) -> bool:
        return bool(self.issues)


@dataclass(frozen=True, slots=True)
class PageSourceAnalysis:
    """Read-only source analysis for one PDF page."""

    page_number: int
    source_kind: PdfSourceKind
    native_text: str
    native_evidence: tuple[TextEvidence, ...]
    raster_regions: tuple[tuple[float, float, float, float], ...]
    raster_coverage: float
    vector_path_count: int
    vector_segments: tuple[LineSegment, ...] = ()


@dataclass(frozen=True, slots=True)
class DocumentSourceAnalysis:
    """Aggregate classification and page evidence for one PDF."""

    source_kind: PdfSourceKind
    pages: tuple[PageSourceAnalysis, ...]


@dataclass(frozen=True, slots=True)
class EngineeringCorrectionCandidate:
    """One non-destructive correction suggestion for a source-text span."""

    raw_text: str
    suggested_text: str
    start: int
    end: int
    kind: EngineeringTokenKind
    reason: str
    confidence: float
    status: EngineeringReviewStatus = EngineeringReviewStatus.REVIEW


@dataclass(frozen=True, slots=True)
class EngineeringNormalizationReport:
    """Raw engineering text plus review candidates; raw text is never overwritten."""

    raw_text: str
    candidates: tuple[EngineeringCorrectionCandidate, ...]

    @property
    def requires_review(self) -> bool:
        return any(
            candidate.status is EngineeringReviewStatus.REVIEW
            for candidate in self.candidates
        )


@dataclass(frozen=True, slots=True)
class EngineeringFusionConfig:
    """Validated thresholds for conservative confidence fusion."""

    pass_threshold: float = 0.78
    low_ocr_threshold: float = 0.60
    auto_correct_threshold: float = 0.98
    allow_auto_correct: bool = False

    def validate(self) -> None:
        values = (
            self.pass_threshold,
            self.low_ocr_threshold,
            self.auto_correct_threshold,
        )
        if any(value < 0.0 or value > 1.0 for value in values):
            raise ValueError("Engineering confidence threshold는 0~1 범위여야 합니다.")
        if self.low_ocr_threshold > self.pass_threshold:
            raise ValueError("low OCR threshold는 pass threshold보다 클 수 없습니다.")
        if self.auto_correct_threshold < self.pass_threshold:
            raise ValueError("auto-correct threshold는 pass threshold 이상이어야 합니다.")


@dataclass(frozen=True, slots=True)
class EngineeringReviewItem:
    """One review-ready item with all evidence retained for audit."""

    text_index: int
    raw_text: str
    bounding_box: tuple[float, float, float, float]
    status: EngineeringReviewStatus
    fused_confidence: float
    reasons: tuple[str, ...]
    correction_candidates: tuple[EngineeringCorrectionCandidate, ...]
    related_symbol_ids: tuple[str, ...]
    external_issues: tuple[CrossSheetValidationIssue, ...]


@dataclass(frozen=True, slots=True)
class EngineeringReviewBundle:
    """Immutable PASS/REVIEW result for one analyzed page or region."""

    items: tuple[EngineeringReviewItem, ...]

    @property
    def review_count(self) -> int:
        return sum(
            item.status is EngineeringReviewStatus.REVIEW for item in self.items
        )


@dataclass(frozen=True, slots=True)
class EngineeringPageBundle:
    """Structured evidence and review output for one source page."""

    source: PageSourceAnalysis
    texts: tuple[TextEvidence, ...]
    review: EngineeringReviewBundle
    relationships: tuple[TextSymbolRelationship, ...] = ()
    values: tuple[EngineeringTextValue, ...] = ()


@dataclass(frozen=True, slots=True)
class EngineeringAnalysisResult:
    """Published read-only engineering analysis summary."""

    input_path: Path
    output_directory: Path
    page_count: int
    text_count: int
    review_count: int
