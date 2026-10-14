"""Data models for revision-aware PDF comparison."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ChangeKind(str, Enum):
    """Visual change classifications used by Checker annotations."""

    ADDED_OR_MODIFIED = "added_or_modified"
    DELETED = "deleted"
    MOVED_OR_RESHAPED = "moved_or_reshaped"


@dataclass(frozen=True, slots=True)
class NormalizedRect:
    """Page-relative rectangle with coordinates in the range 0 through 1."""

    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True, slots=True)
class CheckerRequest:
    """Inputs for comparing an old revision against the latest revision."""

    previous_path: Path
    latest_path: Path
    revision_number: str
    exclusions: tuple[NormalizedRect, ...] = ()
    allow_signature_invalidation: bool = False


@dataclass(frozen=True, slots=True)
class PageMatch:
    """Automatically selected previous/latest page pair."""

    previous_page: int
    latest_page: int
    score: float


@dataclass(frozen=True, slots=True)
class ChangeRegion:
    """One classified change in latest-page PDF coordinates."""

    page_number: int
    rectangle: tuple[float, float, float, float]
    kind: ChangeKind


@dataclass(frozen=True, slots=True)
class CheckerResult:
    """Verified comparison output and diagnostics."""

    previous_path: Path
    latest_path: Path
    output_path: Path
    page_matches: tuple[PageMatch, ...]
    changes: tuple[ChangeRegion, ...]

