"""Search PDF text with optional page-level offline OCR fallback."""

from __future__ import annotations

import re
import logging
import unicodedata
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from slimpdf.core.jobs import CancellationToken, JobCancelledError
from slimpdf.finder_models import (
    FinderFileDiagnostic,
    FinderMatch,
    FinderRequest,
    FinderResult,
    SearchMatchMode,
    SearchTermOperator,
)
from slimpdf.ocr_models import OcrTextResult, OcrWord
from slimpdf.repositories.finder_cache_repository import (
    load_finder_cache,
    save_finder_cache,
)
from slimpdf.services.pdf_security_service import PdfPasswordRequiredError
from slimpdf.services.engineering_ocr_service import recognize_engineering_image
from slimpdf.utils.paths import get_app_data_directory


MAX_FINDER_FILES = 500
MAX_FINDER_PAGES_PER_FILE = 1_000
MAX_FINDER_FILE_BYTES = 2 * 1024 * 1024 * 1024
OCR_TEXT_THRESHOLD = 20
LOW_CONFIDENCE_THRESHOLD = 60.0
EXCERPT_LENGTH = 180
Recognizer = Callable[[Path, tuple[str, ...]], OcrTextResult]
ProgressCallback = Callable[[int, int, Path, int], None]
LOGGER = logging.getLogger(__name__)


class FinderValidationError(ValueError):
    """Raised when Finder input or search options are unsafe or unsupported."""


def collect_pdf_paths(
    selected_paths: Sequence[Path], *, include_subfolders: bool = False
) -> tuple[Path, ...]:
    """Expand explicitly selected files and folders into a stable PDF list."""

    found: dict[str, Path] = {}
    for raw_path in selected_paths:
        path = raw_path.expanduser().resolve()
        candidates = (
            path.rglob("*.pdf") if path.is_dir() and include_subfolders
            else path.glob("*.pdf") if path.is_dir()
            else (path,)
        )
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.casefold() == ".pdf":
                found[str(candidate).casefold()] = candidate
                if len(found) > MAX_FINDER_FILES:
                    raise FinderValidationError(
                        f"PDF FINDER는 한 번에 최대 {MAX_FINDER_FILES}개 파일을 검색할 수 있습니다."
                    )
    return tuple(sorted(found.values(), key=lambda item: str(item).casefold()))


def search_pdfs(
    request: FinderRequest,
    *,
    recognizer: Recognizer = recognize_engineering_image,
    cancellation: CancellationToken | None = None,
    on_progress: ProgressCallback | None = None,
) -> FinderResult:
    """Search every PDF sequentially and retain per-file failures for final reporting."""

    paths, terms = _validate_request(request)
    token = cancellation or CancellationToken()
    matches: list[FinderMatch] = []
    diagnostics: list[FinderFileDiagnostic] = []
    for file_index, path in enumerate(paths):
        if token.is_cancelled:
            return FinderResult(tuple(matches), tuple(diagnostics), cancelled=True)
        try:
            file_matches, diagnostic = _search_one_pdf(
                path,
                request,
                terms,
                recognizer,
                token,
                file_index,
                len(paths),
                on_progress,
            )
        except JobCancelledError:
            return FinderResult(tuple(matches), tuple(diagnostics), cancelled=True)
        except Exception as error:
            LOGGER.warning("PDF Finder skipped %s: %s", path, error)
            diagnostics.append(FinderFileDiagnostic(path, error=str(error)))
        else:
            matches.extend(file_matches)
            diagnostics.append(diagnostic)
    return FinderResult(tuple(matches), tuple(diagnostics))


def _validate_request(request: FinderRequest) -> tuple[tuple[Path, ...], tuple[str, ...]]:
    if not request.input_paths:
        raise FinderValidationError("검색할 PDF를 하나 이상 선택하세요.")
    if len(request.input_paths) > MAX_FINDER_FILES:
        raise FinderValidationError(f"PDF는 최대 {MAX_FINDER_FILES}개까지 검색할 수 있습니다.")
    terms = tuple(term.strip() for term in request.search_terms if term.strip())
    if not terms:
        raise FinderValidationError("검색어를 하나 이상 입력하세요.")
    if request.match_mode is SearchMatchMode.REGEX:
        for term in terms:
            try:
                re.compile(term)
            except re.error as error:
                raise FinderValidationError(f"정규식이 올바르지 않습니다: {term} ({error})") from error
    paths = tuple(path.expanduser().resolve() for path in request.input_paths)
    for path in paths:
        if not path.is_file() or path.suffix.casefold() != ".pdf":
            raise FinderValidationError(f"PDF 파일을 찾을 수 없습니다: {path}")
        if path.stat().st_size <= 0:
            raise FinderValidationError(f"빈 PDF는 검색할 수 없습니다: {path.name}")
        if path.stat().st_size > MAX_FINDER_FILE_BYTES:
            raise FinderValidationError(f"2GB를 초과한 PDF는 검색할 수 없습니다: {path.name}")
    return paths, terms


def _search_one_pdf(
    path: Path,
    request: FinderRequest,
    terms: tuple[str, ...],
    recognizer: Recognizer,
    token: CancellationToken,
    file_index: int,
    file_total: int,
    on_progress: ProgressCallback | None,
) -> tuple[list[FinderMatch], FinderFileDiagnostic]:
    pymupdf = _load_pymupdf()
    document = pymupdf.open(path)
    try:
        was_encrypted = bool(document.needs_pass)
        if was_encrypted and not _authenticate(document, request.credentials):
            raise PdfPasswordRequiredError(
                "암호화 PDF는 회사 승인 자격 증명으로만 FINDER에서 분석할 수 있습니다."
            )
        if len(document) > MAX_FINDER_PAGES_PER_FILE:
            raise FinderValidationError(
                f"파일당 {MAX_FINDER_PAGES_PER_FILE}페이지 제한을 초과했습니다."
            )
        file_matches: list[FinderMatch] = []
        cached_pages = (
            load_finder_cache(path)
            if request.cache_enabled and not was_encrypted
            else None
        )
        updated_cache: list[dict] = []
        ocr_pages: list[int] = []
        low_confidence_pages: list[int] = []
        for page_index, page in enumerate(document):
            token.raise_if_cancelled()
            if on_progress is not None:
                on_progress(file_index, file_total, path, page_index + 1)
            cached = cached_pages[page_index] if cached_pages and page_index < len(cached_pages) else {}
            native_text = (
                str(cached["native_text"])
                if isinstance(cached, dict) and "native_text" in cached
                else page.get_text().strip()
            )
            text = native_text
            used_ocr = False
            ocr_result: OcrTextResult | None = None
            ocr_image_size: tuple[int, int] | None = None
            if request.use_ocr and len(text) < OCR_TEXT_THRESHOLD:
                cached_ocr = cached.get("ocr_text")
                if cached_ocr is None:
                    ocr_result, ocr_image_size = _recognize_page(page, recognizer)
                else:
                    ocr_result = OcrTextResult(
                        tuple(OcrWord(**item) for item in cached.get("ocr_words", ())),
                        str(cached_ocr),
                        float(cached.get("ocr_confidence", 0.0)),
                    )
                    cached_size = cached.get("ocr_image_size") or ()
                    if len(cached_size) == 2:
                        ocr_image_size = (int(cached_size[0]), int(cached_size[1]))
                text = "\n".join(part for part in (text, ocr_result.text) if part)
                used_ocr = True
                ocr_pages.append(page_index + 1)
                if ocr_result.average_confidence < LOW_CONFIDENCE_THRESHOLD:
                    low_confidence_pages.append(page_index + 1)
                updated_cache.append(
                    {
                        "native_text": native_text,
                        "ocr_text": ocr_result.text,
                        "ocr_confidence": ocr_result.average_confidence,
                        "ocr_words": [
                            {
                                "text": word.text,
                                "confidence": word.confidence,
                                "left": word.left,
                                "top": word.top,
                                "width": word.width,
                                "height": word.height,
                            }
                            for word in ocr_result.words
                        ],
                        "ocr_image_size": ocr_image_size,
                    }
                )
            else:
                updated_cache.append(dict(cached) if cached else {"native_text": native_text})
            matched_terms = _matched_terms(text, terms, request)
            required = len(terms) if request.operator is SearchTermOperator.AND else 1
            if len(matched_terms) >= required:
                sentence, rectangle = _locate_match(
                    page,
                    text,
                    matched_terms,
                    request,
                    ocr_result,
                    ocr_image_size,
                )
                file_matches.append(
                    FinderMatch(
                        path,
                        page_index + 1,
                        matched_terms,
                        _build_excerpt(text, matched_terms[0]),
                        used_ocr,
                        sentence,
                        rectangle,
                        (float(page.rect.width), float(page.rect.height)),
                    )
                )
        if request.cache_enabled and not was_encrypted:
            save_finder_cache(path, tuple(updated_cache))
        return file_matches, FinderFileDiagnostic(
            path,
            page_count=len(document),
            ocr_pages=tuple(ocr_pages),
            low_confidence_pages=tuple(low_confidence_pages),
        )
    finally:
        document.close()


def _authenticate(document, credentials) -> bool:
    for credential in credentials:
        if document.authenticate(credential.password):
            return True
    return False


def _recognize_page(
    page, recognizer: Recognizer
) -> tuple[OcrTextResult, tuple[int, int]]:
    scale = 300 / 72.0
    pymupdf = _load_pymupdf()
    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=False
    )
    working_directory = get_app_data_directory() / "cache" / "finder" / "tmp"
    working_directory.mkdir(parents=True, exist_ok=True)
    image_path = working_directory / f"page-{uuid.uuid4().hex}.png"
    try:
        pixmap.save(image_path)
        return recognizer(image_path, ("kor", "eng")), (pixmap.width, pixmap.height)
    finally:
        image_path.unlink(missing_ok=True)


def _load_pymupdf():
    try:
        import pymupdf
    except ImportError as error:
        raise RuntimeError("PyMuPDF가 설치되어 있지 않습니다.") from error
    return pymupdf


def _normalize(value: str, *, case_sensitive: bool, korean_spacing: bool) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if korean_spacing:
        normalized = re.sub(r"(?<=[\uac00-\ud7a3])\s+(?=[\uac00-\ud7a3])", "", normalized)
    return normalized if case_sensitive else normalized.casefold()


def _matched_terms(text: str, terms: tuple[str, ...], request: FinderRequest) -> tuple[str, ...]:
    normalized_text = _normalize(
        text,
        case_sensitive=request.case_sensitive,
        korean_spacing=request.normalize_korean_spacing,
    )
    found: list[str] = []
    for term in terms:
        flags = 0 if request.case_sensitive else re.IGNORECASE
        if request.match_mode is SearchMatchMode.REGEX:
            matched = re.search(term, text, flags) is not None
        else:
            normalized_term = _normalize(
                term,
                case_sensitive=request.case_sensitive,
                korean_spacing=request.normalize_korean_spacing,
            )
            if request.match_mode is SearchMatchMode.EXACT:
                matched = re.search(
                    rf"(?<!\w){re.escape(normalized_term)}(?!\w)", normalized_text
                ) is not None
            else:
                matched = normalized_term in normalized_text
        if matched:
            found.append(term)
    return tuple(found)


def _build_excerpt(text: str, term: str) -> str:
    compact = " ".join(text.split())
    position = compact.casefold().find(term.casefold())
    if position < 0:
        return compact[:EXCERPT_LENGTH]
    start = max(0, position - EXCERPT_LENGTH // 3)
    return compact[start : start + EXCERPT_LENGTH]


def _locate_match(
    page,
    text: str,
    terms: tuple[str, ...],
    request: FinderRequest,
    ocr_result: OcrTextResult | None,
    ocr_image_size: tuple[int, int] | None,
) -> tuple[str, tuple[float, float, float, float] | None]:
    """Return the containing line/sentence and first visual occurrence rectangle."""

    pymupdf = _load_pymupdf()
    if ocr_result is not None and ocr_image_size and ocr_result.words:
        matching_words = [
            word
            for word in ocr_result.words
            if _matched_terms(word.text, terms, request)
        ]
        if matching_words:
            image_width, image_height = ocr_image_size
            x0 = min(word.left for word in matching_words) / image_width * page.rect.width
            y0 = min(word.top for word in matching_words) / image_height * page.rect.height
            x1 = max(word.left + word.width for word in matching_words) / image_width * page.rect.width
            y1 = max(word.top + word.height for word in matching_words) / image_height * page.rect.height
            sentence = _sentence_for_terms(ocr_result.text, terms, request)
            return sentence, (x0, y0, x1, y1)

    for block in page.get_text("dict").get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            line_text = "".join(str(span.get("text", "")) for span in line.get("spans", ())).strip()
            if not line_text or not _matched_terms(line_text, terms, request):
                continue
            rectangle = pymupdf.Rect(line.get("bbox", block.get("bbox")))
            sentence = _sentence_for_terms(text, terms, request, preferred=line_text)
            return sentence, tuple(rectangle)

    for term in terms:
        rectangles = page.search_for(term)
        if rectangles:
            return _sentence_for_terms(text, terms, request), tuple(rectangles[0])
    return _sentence_for_terms(text, terms, request), None


def _sentence_for_terms(
    text: str,
    terms: tuple[str, ...],
    request: FinderRequest,
    *,
    preferred: str = "",
) -> str:
    """Choose a concise sentence or source line containing a matched term."""

    if preferred:
        return " ".join(preferred.split())[:500]
    candidates = [
        item.strip()
        for item in re.split(r"(?<=[.!?。])\s+|[\r\n]+", text)
        if item.strip()
    ]
    for candidate in candidates:
        if _matched_terms(candidate, terms, request):
            return " ".join(candidate.split())[:500]
    return _build_excerpt(text, terms[0])[:500]
