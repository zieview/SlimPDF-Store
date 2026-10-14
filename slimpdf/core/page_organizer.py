"""Verified page-orientation cleanup use case."""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from pathlib import Path

from slimpdf.core.compression import publish_output_without_overwrite, validate_input_path
from slimpdf.page_models import (
    PageOrganizationMode,
    PageOrganizationRequest,
    PageOrganizationResult,
)
from slimpdf.services.pdf_service import (
    SignedPdfNotSupportedError,
    _render_all_output_pages,
    pdf_has_signatures,
)

MIN_DIRECTION_CHARACTERS = 8
MIN_DIRECTION_DOMINANCE = 0.60


class PageOrganizationError(RuntimeError):
    """Raised when page organization cannot produce a verified result."""


def _load_pymupdf():
    try:
        import pymupdf
    except ImportError as error:
        raise PageOrganizationError("PyMuPDF가 설치되어 있지 않습니다.") from error
    return pymupdf


def build_available_organized_path(
    input_path: Path, mode: PageOrganizationMode
) -> Path:
    """Return a mode-specific path without overwriting an existing file."""

    suffix = "_auto_rotated" if mode is PageOrganizationMode.AUTO_TEXT else "_portrait"
    preferred = input_path.with_name(f"{input_path.stem}{suffix}.pdf")
    if not preferred.exists():
        return preferred
    counter = 2
    while True:
        candidate = preferred.with_name(f"{preferred.stem}_{counter}.pdf")
        if not candidate.exists():
            return candidate
        counter += 1


def detect_text_rotation(page) -> int | None:
    """Return the dominant cardinal text rotation, or ``None`` when uncertain."""

    weights: dict[int, int] = defaultdict(int)
    for block in page.get_text("dict").get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            direction = line.get("dir")
            if not direction or len(direction) != 2:
                continue
            characters = sum(
                len(str(span.get("text", "")).strip()) for span in line.get("spans", ())
            )
            if characters <= 0:
                continue
            angle = math.degrees(math.atan2(-float(direction[1]), float(direction[0])))
            cardinal = int(round(angle / 90.0) * 90) % 360
            weights[cardinal] += characters
    total = sum(weights.values())
    if total < MIN_DIRECTION_CHARACTERS or not weights:
        return None
    rotation, dominant = max(weights.items(), key=lambda item: item[1])
    if dominant / total < MIN_DIRECTION_DOMINANCE:
        return None
    return rotation


def organize_pdf_pages(request: PageOrganizationRequest) -> PageOrganizationResult:
    """Rotate pages, validate the result, and publish a separate PDF copy."""

    pymupdf = _load_pymupdf()
    input_path = validate_input_path(request.input_path)
    if pdf_has_signatures(input_path):
        raise SignedPdfNotSupportedError(
            "전자서명이 포함된 PDF는 페이지 회전 시 서명이 무효화되므로 정리하지 않았습니다."
        )
    temporary = input_path.parent / (
        f".{input_path.stem}.organize-{uuid.uuid4().hex}.tmp.pdf"
    )
    document = None
    try:
        document = pymupdf.open(input_path)
        if document.needs_pass:
            raise PageOrganizationError(
                "암호화 PDF는 먼저 'SlimPDF로 PDF 보안 해제'를 실행하세요."
            )
        rotated: list[int] = []
        unchanged: list[int] = []
        uncertain: list[int] = []
        expected_rotations: list[int] = []
        for page_number, page in enumerate(document, start=1):
            current = int(page.rotation) % 360
            target = current
            if request.mode is PageOrganizationMode.AUTO_TEXT:
                detected = detect_text_rotation(page)
                if detected is None:
                    uncertain.append(page_number)
                else:
                    target = detected
            elif page.rect.width > page.rect.height:
                target = (current + 90) % 360
            if target != current:
                page.set_rotation(target)
                rotated.append(page_number)
            else:
                unchanged.append(page_number)
            expected_rotations.append(target)
        document.save(temporary, garbage=3, deflate=True)
        document.close()
        document = None
        _validate_organized_pdf(temporary, expected_rotations)
        while True:
            output_path = build_available_organized_path(input_path, request.mode)
            try:
                publish_output_without_overwrite(temporary, output_path)
                break
            except FileExistsError:
                continue
        warnings = ()
        if uncertain:
            page_list = ", ".join(map(str, uncertain))
            warnings = (f"텍스트 방향을 확정할 수 없어 유지한 페이지: {page_list}",)
        return PageOrganizationResult(
            input_path,
            output_path,
            len(expected_rotations),
            tuple(rotated),
            tuple(unchanged),
            warnings,
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        if document is not None:
            document.close()


def _validate_organized_pdf(output_path: Path, expected_rotations: list[int]) -> None:
    """Reopen every page, verify rotation metadata, and render its content."""

    pymupdf = _load_pymupdf()
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise PageOrganizationError("페이지 정리 결과 PDF가 생성되지 않았습니다.")
    with pymupdf.open(output_path) as document:
        if len(document) != len(expected_rotations):
            raise PageOrganizationError("페이지 정리 중 페이지 수가 변경되었습니다.")
        actual = [int(page.rotation) % 360 for page in document]
        if actual != expected_rotations:
            raise PageOrganizationError("페이지 회전 정보 검증에 실패했습니다.")
    _render_all_output_pages(output_path)
