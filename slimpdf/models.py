"""Data models shared by the application layers."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class CompressionMode(str, Enum):
    """Supported PDF compression modes."""

    MAXIMUM = "최대"

    @classmethod
    def from_cli_value(cls, value: str | None) -> "CompressionMode":
        """Convert an optional command-line value to a compression mode."""

        normalized = (value or "").strip().lower()
        if normalized not in {"", "max"}:
            raise ValueError(f"지원하지 않는 압축 모드입니다: {value}")
        return cls.MAXIMUM


@dataclass(frozen=True, slots=True)
class CompressionDiagnostics:
    """Non-fatal details produced by a compression service."""

    images_processed: int = 0
    images_skipped: int = 0
    images_failed: int = 0
    metadata_cleanup_failed: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompressionRequest:
    """Validated input needed to compress a PDF."""

    input_path: Path
    mode: CompressionMode
    allow_signature_invalidation: bool = False


@dataclass(frozen=True, slots=True)
class CompressionResult:
    """Summary returned after a successful compression."""

    input_path: Path
    output_path: Path
    original_size: int
    compressed_size: int
    compression_ratio: int
    mode: CompressionMode
    diagnostics: CompressionDiagnostics = CompressionDiagnostics()
