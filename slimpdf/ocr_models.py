"""Data models shared by OCR services, use cases, and UI."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class OcrWord:
    """One OCR word positioned in source image pixels."""

    text: str
    confidence: float
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class OcrTextResult:
    """OCR engine output for one rendered page or image region."""

    words: tuple[OcrWord, ...]
    text: str
    average_confidence: float


@dataclass(frozen=True, slots=True)
class OcrRequest:
    """Inputs for creating a searchable PDF and companion text file."""

    input_path: Path
    languages: tuple[str, ...] = ("kor", "eng")
    allow_signature_invalidation: bool = False


@dataclass(frozen=True, slots=True)
class OcrResult:
    """Verified OCR output and page-level diagnostics."""

    input_path: Path
    output_pdf_path: Path
    output_text_path: Path
    page_count: int
    pages_ocrd: int
    low_confidence_pages: tuple[int, ...]

