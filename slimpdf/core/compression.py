"""PDF compression use case independent of the user interface."""

import logging
import os
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from slimpdf.models import CompressionDiagnostics, CompressionMode, CompressionRequest, CompressionResult
from slimpdf.services.ghostscript_service import compress_with_ghostscript
from slimpdf.services.pdf_service import (
    flatten_signatures_to_unsigned_copy,
    pdf_has_signatures,
    validate_pdf_integrity,
)
from slimpdf.utils.paths import build_available_output_path
from slimpdf.utils.temp_paths import managed_temporary_directory

LOGGER = logging.getLogger(__name__)
Compressor = Callable[[Path, Path], CompressionDiagnostics | None]
OutputValidator = Callable[[Path, Path], tuple[int, int]]
SignatureChecker = Callable[[Path], bool]
SignatureFlattener = Callable[[Path, Path], int]


class CompressionValidationError(ValueError):
    """Raised when a requested input cannot be safely compressed."""


class CompressionNotBeneficialError(RuntimeError):
    """Raised when a generated file is not smaller than its source."""


def publish_output_without_overwrite(temporary_output: Path, output_path: Path) -> None:
    """Atomically publish on Windows, failing if the destination already exists."""

    if os.name != "nt":
        raise RuntimeError("안전한 결과 게시는 Windows 파일 시스템에서만 지원됩니다.")
    os.rename(temporary_output, output_path)


def validate_input_path(input_path: Path) -> Path:
    """Validate and normalize a user-selected PDF path."""

    normalized_path = input_path.expanduser().resolve()
    if not normalized_path.exists():
        raise CompressionValidationError(f"PDF 파일을 찾을 수 없습니다: {normalized_path}")
    if not normalized_path.is_file():
        raise CompressionValidationError(f"파일 경로가 아닙니다: {normalized_path}")
    if normalized_path.suffix.lower() != ".pdf":
        raise CompressionValidationError("PDF 파일만 지원됩니다.")
    if normalized_path.stat().st_size <= 0:
        raise CompressionValidationError("빈 PDF 파일은 압축할 수 없습니다.")
    return normalized_path


def calculate_compression_ratio(original_size: int, compressed_size: int) -> int:
    """Return the legacy integer compression percentage."""

    if original_size <= 0:
        raise CompressionValidationError("원본 파일 크기가 올바르지 않습니다.")
    return int((1 - (compressed_size / original_size)) * 100)


def compress_pdf(
    request: CompressionRequest,
    *,
    maximum_compressor: Compressor = compress_with_ghostscript,
    output_validator: OutputValidator = validate_pdf_integrity,
    signature_checker: SignatureChecker = pdf_has_signatures,
    signature_flattener: SignatureFlattener = flatten_signatures_to_unsigned_copy,
) -> CompressionResult:
    """Compress to a temporary PDF, validate it, then publish without overwrite."""

    input_path = validate_input_path(request.input_path)
    signed_pdf = signature_checker(input_path)
    if signed_pdf and not request.allow_signature_invalidation:
        from slimpdf.services.pdf_service import SignedPdfNotSupportedError

        raise SignedPdfNotSupportedError(
            "전자서명이 포함된 PDF는 압축할 수 없습니다. "
            "PDF 바이트 변경 시 서명이 무효화될 수 있어 결과를 생성하지 않았습니다."
        )
    if signed_pdf:
        LOGGER.warning(
            "Signed PDF compression explicitly authorized; signatures will be invalidated: %s",
            input_path,
        )
    original_size = input_path.stat().st_size
    # Keep the same-directory publication stage short.  Repeating a long source
    # stem can push deeply nested UNC paths beyond limits of native PDF tools.
    temporary_output = input_path.parent / f".slimpdf-{uuid.uuid4().hex}.tmp.pdf"
    diagnostics = CompressionDiagnostics()
    compression_input = input_path

    LOGGER.info("Compression started: mode=%s input=%s", request.mode.value, input_path)
    with managed_temporary_directory("slimpdf-signature-") as signature_workspace:
        try:
            if signed_pdf:
                flattened_input = signature_workspace / "unsigned.pdf"
                flattened_count = signature_flattener(input_path, flattened_input)
                compression_input = flattened_input
                LOGGER.info(
                    "Digital signatures flattened before compression: input=%s widgets=%d",
                    input_path,
                    flattened_count,
                )
            if request.mode is not CompressionMode.MAXIMUM:
                raise CompressionValidationError(f"지원하지 않는 압축 모드입니다: {request.mode}")
            if maximum_compressor is compress_with_ghostscript:
                diagnostics = maximum_compressor(
                    compression_input,
                    temporary_output,
                ) or diagnostics
            else:
                diagnostics = maximum_compressor(compression_input, temporary_output) or diagnostics

            if signed_pdf:
                diagnostics = replace(
                    diagnostics,
                    warnings=diagnostics.warnings
                    + (
                        "전자서명 속성은 제거되었으며 서명·주석·스탬프 표시를 페이지 콘텐츠로 평면화한 압축 사본입니다.",
                    ),
                )

            output_validator(compression_input, temporary_output)
            if signed_pdf and signature_checker(temporary_output):
                from slimpdf.services.pdf_service import SignatureFlatteningError

                raise SignatureFlatteningError("압축 결과에 전자서명 속성이 남아 있습니다.")
            compressed_size = temporary_output.stat().st_size
            if compressed_size >= original_size:
                raise CompressionNotBeneficialError(
                    "압축 결과가 원본보다 작지 않아 원본을 유지하고 결과 파일을 저장하지 않았습니다."
                )

            while True:
                output_path = build_available_output_path(input_path, request.mode)
                try:
                    publish_output_without_overwrite(temporary_output, output_path)
                    break
                except FileExistsError:
                    continue
        except Exception:
            temporary_output.unlink(missing_ok=True)
            raise

    result = CompressionResult(
        input_path=input_path,
        output_path=output_path,
        original_size=original_size,
        compressed_size=compressed_size,
        compression_ratio=calculate_compression_ratio(original_size, compressed_size),
        mode=request.mode,
        diagnostics=diagnostics,
    )
    LOGGER.info(
        "Compression completed: output=%s original=%d compressed=%d ratio=%d%%",
        output_path,
        original_size,
        compressed_size,
        result.compression_ratio,
    )
    return result
