"""Read-only engineering drawing analysis pipeline for PDF inputs."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from slimpdf.core.jobs import CancellationToken, JobCancelledError
from slimpdf.engineering_ocr_models import (
    EngineeringAnalysisResult,
    EngineeringFusionConfig,
    EngineeringPageBundle,
    EngineeringTextValue,
    ExternalTagRecord,
    SymbolEvidence,
    TextEvidence,
    TextEvidenceSource,
)
from slimpdf.ocr_models import OcrTextResult
from slimpdf.services.drawing_source_analyzer import (
    analyze_page_source,
    select_ocr_region_boxes,
)
from slimpdf.services.engineering_export_service import export_engineering_analysis
from slimpdf.services.engineering_geometry_service import infer_text_symbol_relationships
from slimpdf.services.engineering_normalization_service import (
    EngineeringDictionary,
    analyze_engineering_text,
)
from slimpdf.services.engineering_numerical_service import (
    build_inline_value_observations,
    validate_numerical_consistency,
)
from slimpdf.services.engineering_ocr_service import recognize_engineering_image
from slimpdf.services.engineering_reference_service import validate_engineering_tags
from slimpdf.services.engineering_review_service import build_engineering_review_bundle
from slimpdf.services.engineering_value_service import extract_engineering_values
from slimpdf.utils.temp_paths import managed_temporary_directory


ENGINEERING_ANALYSIS_DPI = 300
ENGINEERING_DIAGNOSTIC_VERSION = 1
MAX_ENGINEERING_PDF_BYTES = 2 * 1024 * 1024 * 1024
TAG_PATTERN = re.compile(r"\b[A-Z]{1,6}[-_][A-Z0-9]{1,12}\b", re.IGNORECASE)
Recognizer = Callable[[Path, tuple[str, ...]], OcrTextResult]
SymbolDetector = Callable[[object, object], tuple[SymbolEvidence, ...]]
ProgressCallback = Callable[[int, int, str], None]
LOGGER = logging.getLogger(__name__)


class EngineeringAnalysisError(RuntimeError):
    """Raised when a read-only engineering analysis cannot complete safely."""


def analyze_engineering_pdf(
    input_path: Path,
    output_directory: Path,
    *,
    languages: tuple[str, ...] = ("kor", "eng"),
    recognizer: Recognizer = recognize_engineering_image,
    dictionary: EngineeringDictionary | None = None,
    reference_records: tuple[ExternalTagRecord, ...] = (),
    fusion_config: EngineeringFusionConfig = EngineeringFusionConfig(),
    symbol_detector: SymbolDetector | None = None,
    cancellation: CancellationToken | None = None,
    on_progress: ProgressCallback | None = None,
) -> EngineeringAnalysisResult:
    """Analyze a PDF without editing it and publish structured evidence."""

    source = _validate_input(input_path)
    target = output_directory.expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"기존 구조화 결과를 덮어쓸 수 없습니다: {target}")
    if not languages or any(not language.strip() for language in languages):
        raise ValueError("Engineering OCR 언어를 하나 이상 지정해야 합니다.")
    fusion_config.validate()
    token = cancellation or CancellationToken()
    run_id = uuid4().hex[:12]
    started = perf_counter()
    LOGGER.debug(
        "Engineering OCR run started: run_id=%s diagnostic_version=%d "
        "input_bytes=%d dpi=%d languages=%s recognizer=%s",
        run_id,
        ENGINEERING_DIAGNOSTIC_VERSION,
        source.stat().st_size,
        ENGINEERING_ANALYSIS_DPI,
        "+".join(languages),
        getattr(recognizer, "__name__", type(recognizer).__name__),
    )
    pymupdf = _load_pymupdf()
    drafts: list[tuple[object, tuple[TextEvidence, ...], tuple[SymbolEvidence, ...]]] = []
    document = None
    with managed_temporary_directory("slimpdf-ocr-") as workspace:
        try:
            document = pymupdf.open(source)
            if document.needs_pass:
                raise EngineeringAnalysisError(
                    "암호화 PDF는 먼저 'SlimPDF로 PDF 보안 해제'를 실행하세요."
                )
            total_pages = len(document)
            for page_index, page in enumerate(document):
                token.raise_if_cancelled()
                if on_progress:
                    on_progress(page_index, total_pages, f"{page_index + 1}페이지 분석 중")
                page_source = analyze_page_source(page)
                LOGGER.debug(
                    "Engineering OCR page analyzed: run_id=%s page=%d source_kind=%s "
                    "native_items=%d raster_regions=%d vector_paths=%d",
                    run_id,
                    page_index + 1,
                    page_source.source_kind.value,
                    len(page_source.native_evidence),
                    len(page_source.raster_regions),
                    page_source.vector_path_count,
                )
                texts = list(page_source.native_evidence)
                region_boxes = select_ocr_region_boxes(
                    page_source,
                    (page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1),
                )
                for region_index, box in enumerate(region_boxes):
                    token.raise_if_cancelled()
                    region = pymupdf.Rect(box)
                    scale = ENGINEERING_ANALYSIS_DPI / 72.0
                    pixmap = page.get_pixmap(
                        matrix=pymupdf.Matrix(scale, scale),
                        clip=region,
                        alpha=False,
                        annots=False,
                    )
                    image_path = workspace / f"page-{page_index + 1}-region-{region_index + 1}.png"
                    pixmap.save(image_path)
                    region_started = perf_counter()
                    recognized = recognizer(image_path, languages)
                    elapsed_ms = (perf_counter() - region_started) * 1000.0
                    LOGGER.debug(
                        "Engineering OCR region completed: run_id=%s page=%d region=%d "
                        "bbox=%.2f,%.2f,%.2f,%.2f pixels=%dx%d dpi=%d words=%d "
                        "average_confidence=%.2f elapsed_ms=%.1f",
                        run_id,
                        page_index + 1,
                        region_index + 1,
                        region.x0,
                        region.y0,
                        region.x1,
                        region.y1,
                        pixmap.width,
                        pixmap.height,
                        ENGINEERING_ANALYSIS_DPI,
                        len(recognized.words),
                        recognized.average_confidence,
                        elapsed_ms,
                    )
                    texts.extend(
                        _ocr_text_evidence(
                            recognized, region, pixmap.width, pixmap.height
                        )
                    )
                symbols = (
                    symbol_detector(page, page_source) if symbol_detector else ()
                )
                drafts.append((page_source, tuple(texts), tuple(symbols)))
                if on_progress:
                    on_progress(page_index + 1, total_pages, f"{page_index + 1}페이지 완료")
        except (EngineeringAnalysisError, JobCancelledError):
            LOGGER.debug(
                "Engineering OCR run stopped: run_id=%s elapsed_ms=%.1f",
                run_id,
                (perf_counter() - started) * 1000.0,
            )
            raise
        except Exception as error:
            LOGGER.debug(
                "Engineering OCR run failed: run_id=%s stage=source_or_ocr "
                "error_type=%s elapsed_ms=%.1f",
                run_id,
                type(error).__name__,
                (perf_counter() - started) * 1000.0,
            )
            raise EngineeringAnalysisError(f"Engineering PDF 분석에 실패했습니다: {error}") from error
        finally:
            if document is not None:
                document.close()

    observed_tags = tuple(
        match.group(0)
        for _page_source, texts, _symbols in drafts
        for text in texts
        for match in TAG_PATTERN.finditer(text.text)
    )
    LOGGER.debug(
        "Engineering OCR post-processing started: run_id=%s pages=%d "
        "observed_tags=%d reference_records=%d",
        run_id,
        len(drafts),
        len(observed_tags),
        len(reference_records),
    )
    external_report = (
        validate_engineering_tags(observed_tags, reference_records)
        if reference_records
        else None
    )
    pages: list[EngineeringPageBundle] = []
    for page_source, texts, symbols in drafts:
        relationships = infer_text_symbol_relationships(
            texts, symbols, page_source.vector_segments
        )
        normalizations = tuple(
            analyze_engineering_text(text.text, dictionary) for text in texts
        )
        review = build_engineering_review_bundle(
            texts,
            normalizations,
            relationships,
            external_report,
            config=fusion_config,
        )
        values = tuple(
            EngineeringTextValue(index, value)
            for index, text in enumerate(texts)
            for value in extract_engineering_values(text.text)
        )
        pages.append(
            EngineeringPageBundle(page_source, texts, review, relationships, values)
        )
    token.raise_if_cancelled()
    if on_progress:
        on_progress(len(pages), len(pages), "구조화 결과 검증 및 저장 중")
    page_bundle = tuple(pages)
    numerical_observations = build_inline_value_observations(page_bundle)
    LOGGER.debug(
        "Engineering OCR numerical validation started: run_id=%s observations=%d",
        run_id,
        len(numerical_observations),
    )
    numerical_issues = validate_numerical_consistency(numerical_observations)
    LOGGER.debug(
        "Engineering OCR export started: run_id=%s pages=%d numerical_issues=%d",
        run_id,
        len(page_bundle),
        len(numerical_issues),
    )
    published = export_engineering_analysis(
        source,
        page_bundle,
        target,
        numerical_observations=numerical_observations,
        numerical_issues=numerical_issues,
    )
    LOGGER.debug(
        "Engineering OCR run completed: run_id=%s pages=%d texts=%d reviews=%d "
        "numerical_issues=%d elapsed_ms=%.1f",
        run_id,
        len(page_bundle),
        sum(len(page.texts) for page in page_bundle),
        sum(page.review.review_count for page in page_bundle),
        len(numerical_issues),
        (perf_counter() - started) * 1000.0,
    )
    return EngineeringAnalysisResult(
        source,
        published,
        len(pages),
        sum(len(page.texts) for page in pages),
        sum(page.review.review_count for page in pages),
    )


def _ocr_text_evidence(result, region, image_width, image_height):
    if image_width <= 0 or image_height <= 0:
        raise EngineeringAnalysisError("OCR 렌더 이미지 크기가 올바르지 않습니다.")
    scale_x = region.width / image_width
    scale_y = region.height / image_height
    return tuple(
        TextEvidence(
            word.text,
            (
                region.x0 + word.left * scale_x,
                region.y0 + word.top * scale_y,
                region.x0 + (word.left + word.width) * scale_x,
                region.y0 + (word.top + word.height) * scale_y,
            ),
            TextEvidenceSource.OCR,
            max(0.0, min(1.0, word.confidence / 100.0)),
        )
        for word in result.words
    )


def _validate_input(path: Path) -> Path:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Engineering 분석 PDF를 찾을 수 없습니다: {source}")
    if source.suffix.casefold() != ".pdf":
        raise ValueError("Engineering 분석은 PDF 파일만 지원합니다.")
    size = source.stat().st_size
    if size <= 0 or size > MAX_ENGINEERING_PDF_BYTES:
        raise ValueError("Engineering 분석 PDF 크기가 올바르지 않습니다.")
    return source


def _load_pymupdf():
    try:
        import pymupdf
    except ImportError as error:
        raise EngineeringAnalysisError("PyMuPDF가 설치되어 있지 않습니다.") from error
    return pymupdf
