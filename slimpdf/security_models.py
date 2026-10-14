"""Data models for authorized PDF security removal."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PdfCredential:
    """A user-entered or company-approved PDF credential."""

    identifier: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class UnlockRequest:
    """Inputs required to create an unlocked PDF copy."""

    input_path: Path
    credentials: tuple[PdfCredential, ...] = ()
    allow_signature_invalidation: bool = False


@dataclass(frozen=True, slots=True)
class UnlockResult:
    """Verified unlocked output information."""

    input_path: Path
    output_path: Path
    credential_identifier: str
    page_count: int

