"""PDF transformations performed through PyMuPDF, Pillow, and pikepdf."""

import logging
import hashlib
import math
from decimal import Decimal
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from PIL import Image

from slimpdf.config import JPEG_QUALITY
from slimpdf.models import CompressionDiagnostics

LOGGER = logging.getLogger(__name__)


class SignedPdfNotSupportedError(RuntimeError):
    """Raised when PDF modification is requested without signed-PDF consent."""


class SignatureFlatteningError(RuntimeError):
    """Raised when a signature cannot be converted to unsigned page content."""


class PdfIntegrityError(RuntimeError):
    """Raised when a generated PDF fails structural validation."""


class PdfUnsupportedFeatureError(RuntimeError):
    """Raised when an input requires a preservation policy SlimPDF cannot guarantee."""


@dataclass(frozen=True, slots=True)
class PdfFeatureSnapshot:
    page_geometry: tuple[tuple[float, float], ...]
    annotations: tuple[tuple[tuple[tuple[str, str], ...], ...], ...]
    form_fields: tuple[tuple[str, str, str], ...]
    attachments: tuple[tuple[str, int, str], ...]
    encrypted: bool


def _load_pikepdf():
    """Load pikepdf at operation time and provide an actionable error."""

    try:
        import pikepdf
    except ImportError as error:
        raise RuntimeError("pikepdf가 설치되어 있지 않습니다.") from error
    return pikepdf


def _load_pymupdf():
    """Load the PyMuPDF module using its current or legacy import name."""

    try:
        import pymupdf

        return pymupdf
    except ImportError:
        try:
            import fitz

            return fitz
        except ImportError as error:
            raise RuntimeError("PyMuPDF가 설치되어 있지 않습니다.") from error


def create_clean_pdf(input_path: Path, output_path: Path) -> CompressionDiagnostics:
    """Remove unreferenced resources and report optional metadata-cleanup failure."""

    pikepdf = _load_pikepdf()
    metadata_cleanup_failed = False
    with pikepdf.open(input_path) as pdf:
        pdf.remove_unreferenced_resources()
        try:
            for metadata_key in list(pdf.docinfo.keys()):
                del pdf.docinfo[metadata_key]
        except Exception as error:
            metadata_cleanup_failed = True
            LOGGER.warning("PDF metadata removal failed: %s", error)
        pdf.save(output_path)
    warnings = (
        ("문서 메타데이터를 제거하지 못해 작성자 정보 등이 남아 있을 수 있습니다.",)
        if metadata_cleanup_failed
        else ()
    )
    return CompressionDiagnostics(
        metadata_cleanup_failed=metadata_cleanup_failed,
        warnings=warnings,
    )


def pdf_has_signatures(input_path: Path) -> bool:
    """Fail closed when fields, permissions, or indirect objects indicate a signature."""

    pikepdf = _load_pikepdf()
    with pikepdf.open(input_path) as pdf:
        permissions = pdf.Root.get("/Perms")
        if permissions and any(permissions.get(key) is not None for key in ("/DocMDP", "/UR", "/UR3")):
            return True
        acroform = pdf.Root.get("/AcroForm")
        pending = list(acroform.get("/Fields", [])) if acroform else []
        while pending:
            field = pending.pop()
            pending.extend(list(field.get("/Kids", [])))
            if str(field.get("/FT", "")) == "/Sig":
                return True
        try:
            for pdf_object in pdf.objects:
                if not isinstance(pdf_object, pikepdf.Dictionary):
                    continue
                if str(pdf_object.get("/Type", "")) == "/Sig":
                    return True
                signature_markers = ("/ByteRange", "/Contents", "/SubFilter")
                if sum(pdf_object.get(key) is not None for key in signature_markers) >= 2:
                    return True
        except Exception as error:
            LOGGER.warning("PDF signature object inspection was inconclusive; rejecting: %s", error)
            return True
    return False


def flatten_signatures_to_unsigned_copy(input_path: Path, output_path: Path) -> int:
    """Bake annotations/widgets into page content and remove signature data.

    The PDF engine performs the appearance transformation here.  Reconstructing
    it from ``/Rect``, ``/BBox`` and ``/Matrix`` is unsafe on rotated pages and
    can rotate stamps and signature appearances a second time.
    """

    pymupdf = _load_pymupdf()
    flattened = 0
    try:
        document = pymupdf.open(input_path)
        try:
            for page in document:
                flattened += sum(
                    1
                    for widget in (page.widgets() or ())
                    if widget.field_type == pymupdf.PDF_WIDGET_TYPE_SIGNATURE
                )
            document.bake(annots=True, widgets=True)
            document.save(output_path, garbage=4, clean=True, deflate=True)
        finally:
            document.close()
    except Exception as error:
        output_path.unlink(missing_ok=True)
        raise SignatureFlatteningError(
            f"전자서명 표시를 페이지 콘텐츠로 변환하지 못했습니다: {error}"
        ) from error
    if pdf_has_signatures(output_path):
        output_path.unlink(missing_ok=True)
        raise SignatureFlatteningError(
            "전자서명 속성 제거 검증에 실패하여 결과를 생성하지 않았습니다."
        )
    return flattened


def pdf_requires_object_preserving_compression(input_path: Path) -> bool:
    """Return whether Ghostscript could alter interactive PDF objects.

    Ghostscript's pdfwrite device may recreate annotations and thereby change
    borders, colors, opacity, cloud effects, or appearance streams even when
    their visible text and rectangles survive.  Any annotation therefore uses
    the object-preserving compression path, not just AcroForm widgets.
    """

    pikepdf = _load_pikepdf()
    with pikepdf.open(input_path) as pdf:
        if any(page.obj.get("/Annots", []) for page in pdf.pages):
            return True
        acroform = pdf.Root.get("/AcroForm")
        if not acroform:
            return False
        return bool(acroform.get("/Fields", [])) or acroform.get("/XFA") is not None


def _snapshot_form_fields(acroform) -> tuple[tuple[str, str, str], ...]:
    if not acroform:
        return ()
    fields = []
    pending = list(acroform.get("/Fields", []))
    while pending:
        field = pending.pop()
        children = list(field.get("/Kids", []))
        if children:
            pending.extend(children)
        else:
            fields.append(
                (str(field.get("/T", "")), str(field.get("/FT", "")), str(field.get("/V", "")))
            )
    return tuple(sorted(fields))


def _normalize_pdf_value(value):
    """Normalize equivalent PDF scalar and array encodings for comparison."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return round(float(value), 6)
    object_type = getattr(value, "_type_code", None)
    if isinstance(value, (list, tuple)) or getattr(object_type, "name", None) == "array":
        return tuple(_normalize_pdf_value(item) for item in value)
    if getattr(object_type, "name", None) == "dictionary":
        return tuple(
            sorted(
                (str(key), _normalize_pdf_value(child))
                for key, child in value.items()
            )
        )
    return str(value)


def _display_rect(rect, crop_box, rotation: int) -> tuple[float, float, float, float]:
    """Transform a PDF rectangle into normalized displayed-page coordinates."""

    x0, y0, x1, y1 = (float(value) for value in crop_box)
    width, height = abs(x1 - x0), abs(y1 - y0)
    points = []
    for x, y in ((float(rect[0]), float(rect[1])), (float(rect[2]), float(rect[3]))):
        local_x, local_y = x - min(x0, x1), y - min(y0, y1)
        if rotation == 90:
            transformed = (local_y, width - local_x)
        elif rotation == 180:
            transformed = (width - local_x, height - local_y)
        elif rotation == 270:
            transformed = (height - local_y, local_x)
        else:
            transformed = (local_x, local_y)
        points.append(transformed)
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    return tuple(round(value, 6) for value in (min_x, min_y, max_x, max_y))


def _snapshot_annotations(page) -> tuple[tuple[tuple[str, object], ...], ...]:
    keys = (
        "/Subtype", "/Contents", "/NM", "/T", "/Subj", "/Rect", "/C", "/IC",
        "/F", "/Border", "/BS", "/BE", "/CA", "/ca", "/Open", "/Name",
        "/Vertices", "/InkList", "/QuadPoints", "/L", "/LE", "/DA", "/Q",
        "/RC", "/Rotate", "/AS", "/IT", "/BM", "/M", "/CreationDate",
    )
    media_box = page.obj.get("/MediaBox", [0, 0, 0, 0])
    crop_box = page.obj.get("/CropBox", media_box)
    rotation = int(page.obj.get("/Rotate", 0)) % 360

    def snapshot_value(annotation, key: str):
        value = annotation.get(key, "")
        if key == "/Rect" and value:
            return _display_rect(value, crop_box, rotation)
        return _normalize_pdf_value(value)

    return tuple(
        sorted(
            tuple((key, snapshot_value(annotation, key)) for key in keys)
            + (("/AP_PRESENT", annotation.get("/AP") is not None),)
            for annotation in page.obj.get("/Annots", [])
        )
    )


def _snapshot_pdf_features(pdf) -> PdfFeatureSnapshot:
    geometry = []
    annotations = []
    for page in pdf.pages:
        media_box = page.obj.get("/MediaBox", [0, 0, 0, 0])
        crop_box = page.obj.get("/CropBox", media_box)
        if len(crop_box) != 4:
            raise PdfIntegrityError("PDF 페이지 표시 영역 정보를 읽을 수 없습니다.")
        width = abs(float(crop_box[2]) - float(crop_box[0]))
        height = abs(float(crop_box[3]) - float(crop_box[1]))
        rotation = int(page.obj.get("/Rotate", 0)) % 360
        if rotation in (90, 270):
            width, height = height, width
        geometry.append((width, height))
        annotations.append(_snapshot_annotations(page))
    attachments = []
    for name in sorted(pdf.attachments.keys()):
        data = pdf.attachments[name].get_file().read_bytes()
        attachments.append((str(name), len(data), hashlib.sha256(data).hexdigest()))
    return PdfFeatureSnapshot(
        page_geometry=tuple(geometry),
        annotations=tuple(annotations),
        form_fields=_snapshot_form_fields(pdf.Root.get("/AcroForm")),
        attachments=tuple(attachments),
        encrypted=bool(pdf.is_encrypted),
    )


def _page_geometry_matches(
    input_geometry: tuple[tuple[float, float], ...],
    output_geometry: tuple[tuple[float, float], ...],
) -> bool:
    """Compare displayed page sizes while allowing equivalent PDF encodings."""

    if len(input_geometry) != len(output_geometry):
        return False
    return all(
        math.isclose(input_width, output_width, rel_tol=0.0, abs_tol=0.05)
        and math.isclose(input_height, output_height, rel_tol=0.0, abs_tol=0.05)
        for (input_width, input_height), (output_width, output_height) in zip(
            input_geometry, output_geometry, strict=True
        )
    )


def _annotation_rect_matches(input_rect: object, output_rect: object) -> bool:
    """Compare visually equivalent annotation rectangles with PDF rounding tolerance."""

    if not isinstance(input_rect, tuple) or not isinstance(output_rect, tuple):
        return input_rect == output_rect
    if len(input_rect) != 4 or len(output_rect) != 4:
        return input_rect == output_rect
    return all(
        math.isclose(float(input_value), float(output_value), rel_tol=0.0, abs_tol=0.02)
        for input_value, output_value in zip(input_rect, output_rect, strict=True)
    )


def _annotations_match(
    input_annotations: tuple[tuple[tuple[tuple[str, object], ...], ...], ...],
    output_annotations: tuple[tuple[tuple[tuple[str, object], ...], ...], ...],
) -> bool:
    """Compare annotation contents exactly while tolerating harmless coordinate rounding."""

    if len(input_annotations) != len(output_annotations):
        return False
    for input_page, output_page in zip(input_annotations, output_annotations, strict=True):
        if len(input_page) != len(output_page):
            return False
        unmatched = [dict(annotation) for annotation in output_page]
        for input_annotation in input_page:
            expected = dict(input_annotation)
            expected_rect = expected.pop("/Rect", "")
            for index, candidate in enumerate(unmatched):
                candidate_without_rect = dict(candidate)
                candidate_rect = candidate_without_rect.pop("/Rect", "")
                if expected != candidate_without_rect:
                    continue
                if not _annotation_rect_matches(expected_rect, candidate_rect):
                    continue
                unmatched.pop(index)
                break
            else:
                return False
        if unmatched:
            return False
    return True


def _render_all_output_pages(output_path: Path) -> None:
    pymupdf = _load_pymupdf()
    try:
        with pymupdf.open(output_path) as document:
            for page_number, page in enumerate(document, start=1):
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(0.1, 0.1), alpha=False)
                if pixmap.width <= 0 or pixmap.height <= 0:
                    raise PdfIntegrityError(f"결과 PDF {page_number}페이지를 렌더링할 수 없습니다.")
    except PdfIntegrityError:
        raise
    except Exception as error:
        raise PdfIntegrityError(f"결과 PDF 페이지 렌더링에 실패했습니다: {error}") from error


def _snapshot_stamp_appearances(pdf_path: Path) -> tuple[tuple[tuple[object, ...], ...], ...]:
    """Capture rendered stamp appearances independently of PDF stream encoding."""

    pymupdf = _load_pymupdf()
    pages = []
    try:
        with pymupdf.open(pdf_path) as document:
            for page in document:
                stamps = []
                for annotation in page.annots() or ():
                    if annotation.type[1] != "Stamp":
                        continue
                    pixmap = annotation.get_pixmap(
                        matrix=pymupdf.Matrix(1.5, 1.5), alpha=True
                    )
                    rectangle = tuple(round(float(value), 3) for value in annotation.rect)
                    stamps.append(
                        (
                            rectangle,
                            pixmap.width,
                            pixmap.height,
                            hashlib.sha256(pixmap.samples).hexdigest(),
                        )
                    )
                pages.append(tuple(sorted(stamps)))
    except Exception as error:
        raise PdfIntegrityError(f"PDF 스탬프 표시를 검증할 수 없습니다: {error}") from error
    return tuple(pages)


def validate_pdf_integrity(input_path: Path, output_path: Path) -> tuple[int, int]:
    """Validate structure, renderability, and explicitly preserved PDF features."""

    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise PdfIntegrityError("생성된 PDF가 비어 있거나 존재하지 않습니다.")
    pikepdf = _load_pikepdf()
    try:
        with pikepdf.open(input_path) as input_pdf:
            input_pages = len(input_pdf.pages)
            input_features = _snapshot_pdf_features(input_pdf)
        with pikepdf.open(output_path) as output_pdf:
            output_pages = len(output_pdf.pages)
            output_features = _snapshot_pdf_features(output_pdf)
    except Exception as error:
        raise PdfIntegrityError(f"생성된 PDF를 다시 열 수 없습니다: {error}") from error
    if input_pages <= 0 or output_pages != input_pages:
        raise PdfIntegrityError(
            f"PDF 페이지 수가 일치하지 않습니다: 원본 {input_pages}, 결과 {output_pages}"
        )
    if input_features.encrypted:
        raise PdfUnsupportedFeatureError(
            "암호화 PDF는 암호화 속성을 안전하게 보존할 수 없어 압축하지 않습니다."
        )
    if output_features.encrypted != input_features.encrypted:
        raise PdfIntegrityError("PDF 암호화 속성이 변경되었습니다.")
    if not _page_geometry_matches(
        input_features.page_geometry, output_features.page_geometry
    ):
        raise PdfIntegrityError("PDF 페이지 크기 또는 회전 정보가 변경되었습니다.")
    if not _annotations_match(input_features.annotations, output_features.annotations):
        raise PdfIntegrityError("PDF 주석 속성 또는 내용이 변경되었습니다.")
    if _snapshot_stamp_appearances(input_path) != _snapshot_stamp_appearances(output_path):
        raise PdfIntegrityError("PDF 스탬프 모양 또는 표시 속성이 변경되었습니다.")
    if output_features.form_fields != input_features.form_fields:
        raise PdfIntegrityError("PDF 폼 필드 이름, 타입 또는 값이 변경되었습니다.")
    if output_features.attachments != input_features.attachments:
        raise PdfIntegrityError("PDF 첨부파일 이름 또는 내용이 변경되었습니다.")
    _render_all_output_pages(output_path)
    return input_pages, output_pages


def _recompress_page_images(document) -> CompressionDiagnostics:
    """Recompress supported images and report skipped or failed streams."""

    processed = 0
    skipped = 0
    failed = 0

    for page_number, page in enumerate(document, start=1):
        for image_info in page.get_images(full=True):
            xref = image_info[0]
            base_image = document.extract_image(xref)
            if not base_image or "image" not in base_image:
                skipped += 1
                continue
            if base_image.get("ext", "jpg").lower() in {"jp2", "jb2"}:
                skipped += 1
                continue
            try:
                with Image.open(BytesIO(base_image["image"])) as source_image:
                    source_image.load()
                    if source_image.mode == "RGBA":
                        converted_image = Image.new("RGB", source_image.size, (255, 255, 255))
                        converted_image.paste(source_image, mask=source_image.getchannel("A"))
                    elif source_image.mode not in ("RGB", "L"):
                        converted_image = source_image.convert("RGB")
                    else:
                        converted_image = source_image.copy()

                with BytesIO() as buffer:
                    converted_image.save(buffer, format="JPEG", quality=JPEG_QUALITY)
                    document.update_stream(xref, buffer.getvalue())
                converted_image.close()
                processed += 1
            except (OSError, ValueError, RuntimeError) as error:
                failed += 1
                LOGGER.warning(
                    "Image recompression skipped: page=%d xref=%s reason=%s",
                    page_number,
                    xref,
                    error,
                )

    warnings = ()
    if failed:
        warnings = (f"이미지 {failed}개를 처리하지 못해 원본 스트림을 유지했습니다.",)
    return CompressionDiagnostics(
        images_processed=processed,
        images_skipped=skipped,
        images_failed=failed,
        warnings=warnings,
    )


def compress_preserving_pdf_objects(input_path: Path, output_path: Path) -> CompressionDiagnostics:
    """Losslessly compress streams while retaining annotations and form objects.

    This path intentionally avoids PyMuPDF document reconstruction.  pikepdf
    rewrites container and Flate encoding only, so annotation dictionaries and
    their appearance streams retain their PDF semantics.
    """

    pikepdf = _load_pikepdf()
    try:
        with pikepdf.open(input_path) as pdf:
            pdf.save(
                output_path,
                compress_streams=True,
                recompress_flate=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
            )
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return CompressionDiagnostics()


def compress_images(
    input_path: Path,
    output_path: Path,
    *,
    allow_signature_invalidation: bool = False,
) -> CompressionDiagnostics:
    """Recompress images, requiring explicit consent for a signed PDF."""

    pymupdf = _load_pymupdf()
    pikepdf = _load_pikepdf()
    if pdf_has_signatures(input_path) and not allow_signature_invalidation:
        raise SignedPdfNotSupportedError(
            "전자서명이 포함된 PDF는 이미지 재압축할 수 없습니다. "
            "PDF 바이트 변경 시 서명이 무효화될 수 있어 결과를 생성하지 않았습니다."
        )
    temp_path = input_path.parent / f".{input_path.stem}.image-{uuid.uuid4().hex}.tmp.pdf"
    try:
        document = pymupdf.open(input_path)
        try:
            diagnostics = _recompress_page_images(document)
            document.save(temp_path, deflate=True, garbage=4, clean=True)
        finally:
            document.close()

        with pikepdf.open(temp_path) as pdf:
            pdf.save(output_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return diagnostics
