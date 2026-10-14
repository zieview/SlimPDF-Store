"""Data models for PDF page orientation cleanup."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class PageOrganizationMode(str, Enum):
    """Supported page-orientation cleanup modes."""

    AUTO_TEXT = "auto-text"
    PORTRAIT = "portrait"


@dataclass(frozen=True, slots=True)
class PageOrganizationRequest:
    """Input required to create an organized PDF copy."""

    input_path: Path
    mode: PageOrganizationMode


@dataclass(frozen=True, slots=True)
class PageOrganizationResult:
    """Summary of a verified page organization operation."""

    input_path: Path
    output_path: Path
    page_count: int
    rotated_pages: tuple[int, ...]
    unchanged_pages: tuple[int, ...]
    warnings: tuple[str, ...] = ()
