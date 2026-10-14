"""Create verified searchable PDF and TXT outputs with offline OCR."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from io import BytesIO
from pathlib import Path

from PIL import Image

from slimpdf.core.compression import publish_output_without_overwrite
from slimpdf.core.jobs import CancellationToken
from slimpdf.ocr_models import OcrRequest, OcrResult, OcrTextResult
from slimpdf.services.pdf_service import (
    SignedPdfNotSupportedError,
    _render_all_output_pages,
    pdf_has_signatures,
    validate_pdf_integrity,
)
from slimpdf.services.drawing_source_analyzer import (
    analyze_page_source,
    select_ocr_region_boxes,
)
from slimpdf.services.engineering_ocr_service import recognize_engineering_image


SUPPORTED_OCR_INPUTS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}
OCR_DPI = 300
LOW_CONFIDENCE_THRESHOLD = 60.0
Recognizer = Callable[[Path, tuple[str, ...]], OcrTextResult]


class OcrValidationError(ValueError):
    """Raised when an OCR input or generated output is invalid."""


def _load_pymupdf():
    try:
        import pymupdf
    except ImportError as error:
        raise RuntimeError("PyMuPDF가 설치되어 있지 않습니다.") from error
    return pymupdf


def build_available_ocr_paths(input_path: Path) -> tuple[Path, Path]:
    """Choose matching PDF and TXT names without overwriting either file."""

    counter = 1
    while True:
        suffix = "_ocr" if counter == 1 else f"_ocr_{counter}"
        pdf_path = input_path.with_name(f"{input_path.stem}{suffix}.pdf")
        text_path = input_path.with_name(f"{input_path.stem}{suffix}.txt")
        if not pdf_path.exists() and not text_path.exists():
            return pdf_path, text_path
        counter += 1


def validate_ocr_input(input_path: Path) -> Path:
    """Validate PDF or image input without following a nonexistent path."""

    normalized = input_path.expanduser().resolve()
    if not normalized.is_file():
        raise OcrValidationError(f"OCR 입력 파일을 찾을 수 없습니다: {normalized}")
    if normalized.suffix.lower() not in SUPPORTED_OCR_INPUTS:
        raise OcrValidationError("PDF, JPG, PNG, BMP 또는 TIFF 파일만 OCR할 수 있습니다.")
    if normalized.stat().st_size <= 0:
        raise OcrValidationError("빈 파일은 OCR할 수 없습니다.")
    return normalized


def analyze_ocr(
    request: OcrRequest,
    *,
    recognizer: Recognizer = recognize_engineering_image,
    cancellation: CancellationToken | None = None,
) -> OcrResult:
    """OCR scanned pages and image regions, preserving original visible content."""

    pymupdf = _load_pymupdf()
    input_path = validate_ocr_input(request.input_path)
    token = cancellation or CancellationToken()
    is_pdf = input_path.suffix.lower() == ".pdf"
    if is_pdf and pdf_has_signatures(input_path) and not request.allow_signature_invalidation:
        raise SignedPdfNotSupportedError(
            "전자서명이 포함된 PDF입니다. OCR 텍스트 추가로 서명이 무효화될 수 있어 결과를 생성하지 않았습니다."
        )
    temporary_pdf = input_path.parent / f".{input_path.stem}.ocr-{uuid.uuid4().hex}.tmp.pdf"
    temporary_text = input_path.parent / f".{input_path.stem}.ocr-{uuid.uuid4().hex}.tmp.txt"
    temporary_images: list[Path] = []
    published_text: Path | None = None
    document = None
    try:
        if is_pdf:
            document = pymupdf.open(input_path)
            if document.needs_pass:
                raise OcrValidationError(
                    "암호화 PDF는 먼저 'SlimPDF로 PDF 보안 해제'를 실행하세요."
                )
        else:
            document = _create_image_pdf(pymupdf, input_path)

        text_sections: list[str] = []
        low_confidence_pages: list[int] = []
        pages_ocrd = 0
        for page_index, page in enumerate(document):
            token.raise_if_cancelled()
            source_analysis = analyze_page_source(page)
            existing_text = source_analysis.native_text.strip()
            region_boxes = select_ocr_region_boxes(
                source_analysis,
                (page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1),
            )
            regions = tuple(type(page.rect)(box) for box in region_boxes)
            page_ocr_text: list[str] = []
            confidences: list[float] = []
            for region_index, region in enumerate(regions):
                token.raise_if_cancelled()
                image_path = input_path.parent / (
                    f".{input_path.stem}.ocr-{uuid.uuid4().hex}-{page_index}-{region_index}.png"
                )
                temporary_images.append(image_path)
                scale = OCR_DPI / 72.0
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(scale, scale),
                    clip=region,
                    alpha=False,
                    annots=False,
                )
                pixmap.save(image_path)
                recognized = recognizer(image_path, request.languages)
                _insert_invisible_words(page, region, pixmap.width, pixmap.height, recognized)
                if recognized.text:
                    page_ocr_text.append(recognized.text)
                if recognized.words:
                    confidences.append(recognized.average_confidence)
            if regions:
                pages_ocrd += 1
                if not confidences or sum(confidences) / len(confidences) < LOW_CONFIDENCE_THRESHOLD:
                    low_confidence_pages.append(page_index + 1)
            combined = "\n".join(part for part in (existing_text, *page_ocr_text) if part)
            text_sections.append(f"--- Page {page_index + 1} ---\n{combined}".rstrip())

        token.raise_if_cancelled()
        document.save(temporary_pdf, garbage=4, deflate=True)
        document.close()
        document = None
        temporary_text.write_text("\n\n".join(text_sections) + "\n", encoding="utf-8")
        _validate_ocr_output(input_path, temporary_pdf, is_pdf, len(text_sections))

        while True:
            output_pdf, output_text = build_available_ocr_paths(input_path)
            try:
                publish_output_without_overwrite(temporary_text, output_text)
                published_text = output_text
                publish_output_without_overwrite(temporary_pdf, output_pdf)
                break
            except FileExistsError:
                if published_text is not None:
                    published_text.unlink(missing_ok=True)
                    published_text = None
                continue
            except Exception:
                if published_text is not None:
                    published_text.unlink(missing_ok=True)
                raise
        return OcrResult(
            input_path=input_path,
            output_pdf_path=output_pdf,
            output_text_path=output_text,
            page_count=len(text_sections),
            pages_ocrd=pages_ocrd,
            low_confidence_pages=tuple(low_confidence_pages),
        )
    except Exception:
        temporary_pdf.unlink(missing_ok=True)
        temporary_text.unlink(missing_ok=True)
        raise
    finally:
        if document is not None:
            document.close()
        for image_path in temporary_images:
            image_path.unlink(missing_ok=True)


def _create_image_pdf(pymupdf, image_path: Path):
    with Image.open(image_path) as image:
        frame_count = int(getattr(image, "n_frames", 1))
        document = pymupdf.open()
        for frame_index in range(frame_count):
            image.seek(frame_index)
            frame = image.convert("RGB")
            width, height = frame.size
            if width <= 0 or height <= 0:
                document.close()
                raise OcrValidationError("이미지 크기가 올바르지 않습니다.")
            scale = min(1.0, 1440.0 / max(width, height))
            page = document.new_page(width=width * scale, height=height * scale)
            with BytesIO() as buffer:
                frame.save(buffer, format="PNG")
                page.insert_image(page.rect, stream=buffer.getvalue())
    return document


def _insert_invisible_words(page, region, image_width: int, image_height: int, result: OcrTextResult) -> None:
    if image_width <= 0 or image_height <= 0:
        return
    scale_x = region.width / image_width
    scale_y = region.height / image_height
    for word in result.words:
        point = (
            region.x0 + word.left * scale_x,
            region.y0 + (word.top + word.height) * scale_y,
        )
        font_size = max(3.0, word.height * scale_y * 0.82)
        page.insert_text(
            point,
            word.text,
            fontsize=font_size,
            fontname="korea",
            render_mode=3,
            overlay=True,
        )


def _validate_ocr_output(
    input_path: Path, output_path: Path, input_is_pdf: bool, expected_pages: int
) -> None:
    pymupdf = _load_pymupdf()
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise OcrValidationError("OCR 결과 PDF가 생성되지 않았습니다.")
    if input_is_pdf:
        validate_pdf_integrity(input_path, output_path)
    else:
        with pymupdf.open(output_path) as document:
            if len(document) != expected_pages:
                raise OcrValidationError("OCR 결과 PDF 페이지 수가 올바르지 않습니다.")
    _render_all_output_pages(output_path)
