"""Data contracts for the read-only PDF Finder feature."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from slimpdf.security_models import PdfCredential


class SearchMatchMode(str, Enum):
    """Supported text matching strategies."""

    CONTAINS = "contains"
    EXACT = "exact"
    REGEX = "regex"


class SearchTermOperator(str, Enum):
    """How multiple search terms are combined per page."""

    AND = "and"
    OR = "or"


@dataclass(frozen=True, slots=True)
class FinderRequest:
    """Validated inputs for one multi-PDF search."""

    input_paths: tuple[Path, ...]
    search_terms: tuple[str, ...]
    match_mode: SearchMatchMode = SearchMatchMode.CONTAINS
    operator: SearchTermOperator = SearchTermOperator.OR
    case_sensitive: bool = False
    normalize_korean_spacing: bool = True
    use_ocr: bool = True
    credentials: tuple[PdfCredential, ...] = ()
    cache_enabled: bool = False


@dataclass(frozen=True, slots=True)
class FinderMatch:
    """One page containing the requested term or terms."""

    input_path: Path
    page_number: int
    matched_terms: tuple[str, ...]
    excerpt: str
    used_ocr: bool = False
    sentence: str = ""
    rectangle: tuple[float, float, float, float] | None = None
    page_size: tuple[float, float] | None = None


@dataclass(frozen=True, slots=True)
class FinderFileDiagnostic:
    """Non-fatal information for one searched PDF."""

    input_path: Path
    page_count: int = 0
    ocr_pages: tuple[int, ...] = ()
    low_confidence_pages: tuple[int, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class FinderResult:
    """Complete result of one Finder search."""

    matches: tuple[FinderMatch, ...]
    diagnostics: tuple[FinderFileDiagnostic, ...]
    cancelled: bool = False
