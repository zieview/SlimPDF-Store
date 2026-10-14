"""Authorized PDF decryption and preservation validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from slimpdf.security_models import PdfCredential
from slimpdf.services.pdf_service import (
    PdfIntegrityError,
    _annotations_match,
    _page_geometry_matches,
    _render_all_output_pages,
    _snapshot_pdf_features,
)


class PdfPasswordRequiredError(RuntimeError):
    """Raised when none of the explicitly supplied credentials opens a PDF."""


class PdfSecurityUnsupportedError(RuntimeError):
    """Raised when a PDF protection type cannot be safely removed."""


@dataclass(frozen=True, slots=True)
class DecryptionResult:
    """Non-secret details returned after a verified decryption."""

    credential_identifier: str
    page_count: int


def _load_pikepdf():
    try:
        import pikepdf
    except ImportError as error:
        raise RuntimeError("pikepdf가 설치되어 있지 않습니다.") from error
    return pikepdf


def decrypt_pdf_to_copy(
    input_path: Path,
    output_path: Path,
    credentials: tuple[PdfCredential, ...] = (),
) -> DecryptionResult:
    """Decrypt with empty or supplied credentials and verify preserved PDF objects."""

    pikepdf = _load_pikepdf()
    candidates = (PdfCredential("empty-password", ""), *credentials)
    source_pdf = None
    selected_identifier = ""
    for credential in candidates:
        try:
            source_pdf = pikepdf.open(input_path, password=credential.password)
        except pikepdf.PasswordError:
            continue
        selected_identifier = credential.identifier
        break
    if source_pdf is None:
        raise PdfPasswordRequiredError(
            "제공된 암호 또는 회사 승인 자격 증명으로 PDF를 열 수 없습니다."
        )

    try:
        if not source_pdf.is_encrypted:
            raise PdfSecurityUnsupportedError("선택한 PDF에는 제거할 암호화가 없습니다.")
        source_pages = len(source_pdf.pages)
        source_features = _snapshot_pdf_features(source_pdf)
        source_pdf.save(output_path)
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    finally:
        source_pdf.close()

    try:
        with pikepdf.open(output_path) as unlocked_pdf:
            if unlocked_pdf.is_encrypted:
                raise PdfIntegrityError("결과 PDF의 암호화가 제거되지 않았습니다.")
            if len(unlocked_pdf.pages) != source_pages:
                raise PdfIntegrityError("보안 해제 전후의 페이지 수가 다릅니다.")
            output_features = _snapshot_pdf_features(unlocked_pdf)
        if not _page_geometry_matches(
            source_features.page_geometry, output_features.page_geometry
        ):
            raise PdfIntegrityError("보안 해제 과정에서 페이지 크기 또는 회전이 변경됐습니다.")
        if not _annotations_match(source_features.annotations, output_features.annotations):
            raise PdfIntegrityError("보안 해제 과정에서 PDF 주석이 변경됐습니다.")
        if source_features.form_fields != output_features.form_fields:
            raise PdfIntegrityError("보안 해제 과정에서 PDF 폼 필드가 변경됐습니다.")
        if source_features.attachments != output_features.attachments:
            raise PdfIntegrityError("보안 해제 과정에서 PDF 첨부파일이 변경됐습니다.")
        _render_all_output_pages(output_path)
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return DecryptionResult(selected_identifier, source_pages)

