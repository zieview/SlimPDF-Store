"""Classify vector, scanned and hybrid PDF pages without rasterizing them."""

from __future__ import annotations

from collections.abc import Iterable

from slimpdf.engineering_ocr_models import (
    DocumentSourceAnalysis,
    GeometryEvidenceSource,
    LineSegment,
    PageSourceAnalysis,
    PdfSourceKind,
    TextEvidence,
    TextEvidenceSource,
)


def analyze_page_source(page, *, include_vector_geometry: bool = True) -> PageSourceAnalysis:
    """Extract native text and raster regions with explicit provenance."""

    page_dict = page.get_text("dict")
    evidence: list[TextEvidence] = []
    raster_regions: list[tuple[float, float, float, float]] = []
    for block in page_dict.get("blocks", ()):
        block_type = block.get("type")
        if block_type == 0:
            for line in block.get("lines", ()):
                direction = line.get("dir", (1.0, 0.0))
                rotation = _direction_rotation(direction)
                for span in line.get("spans", ()):
                    text = str(span.get("text", "")).strip()
                    bbox = _valid_bbox(span.get("bbox"))
                    if text and bbox is not None:
                        evidence.append(
                            TextEvidence(
                                text=text,
                                bounding_box=bbox,
                                source=TextEvidenceSource.NATIVE_PDF,
                                confidence=1.0,
                                font_name=str(span.get("font", "")) or None,
                                font_size=_optional_float(span.get("size")),
                                rotation=rotation,
                            )
                        )
        elif block_type == 1:
            bbox = _valid_bbox(block.get("bbox"))
            if bbox is not None:
                raster_regions.append(bbox)
    native_text = "\n".join(item.text for item in evidence)
    page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
    raster_area = sum(
        max(0.0, x1 - x0) * max(0.0, y1 - y0)
        for x0, y0, x1, y1 in raster_regions
    )
    coverage = min(1.0, raster_area / page_area)
    vector_count = 0
    vector_segments: tuple[LineSegment, ...] = ()
    if include_vector_geometry:
        try:
            drawings = page.get_drawings()
            vector_count = len(drawings)
            vector_segments = _extract_vector_segments(drawings)
        except (AttributeError, RuntimeError, ValueError):
            vector_count = 0
    kind = classify_page_source(bool(evidence), bool(raster_regions), vector_count)
    return PageSourceAnalysis(
        page.number + 1,
        kind,
        native_text,
        tuple(evidence),
        tuple(raster_regions),
        coverage,
        vector_count,
        vector_segments,
    )


def analyze_document_source(document) -> DocumentSourceAnalysis:
    """Analyze every page and derive one conservative document classification."""

    pages = tuple(analyze_page_source(page) for page in document)
    kinds = {page.source_kind for page in pages}
    if len(kinds) == 1:
        kind = next(iter(kinds))
    else:
        kind = PdfSourceKind.HYBRID
    return DocumentSourceAnalysis(kind, pages)


def classify_page_source(
    has_native_text: bool, has_raster_images: bool, vector_path_count: int
) -> PdfSourceKind:
    """Classify from independent text, raster and vector evidence."""

    if has_native_text and has_raster_images:
        return PdfSourceKind.HYBRID
    if has_native_text or vector_path_count > 0:
        return PdfSourceKind.VECTOR
    return PdfSourceKind.SCANNED


def select_ocr_region_boxes(
    analysis: PageSourceAnalysis,
    page_box: tuple[float, float, float, float],
    *,
    native_text_threshold: int = 20,
    minimum_raster_coverage: float = 0.02,
) -> tuple[tuple[float, float, float, float], ...]:
    """Preserve the established OCR selection policy using analyzed evidence."""

    if len(analysis.native_text) < native_text_threshold:
        return (page_box,)
    page_area = max(1.0, (page_box[2] - page_box[0]) * (page_box[3] - page_box[1]))
    return tuple(
        box
        for box in analysis.raster_regions
        if max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1]) / page_area
        >= minimum_raster_coverage
    )


def _valid_bbox(value: Iterable[object] | None) -> tuple[float, float, float, float] | None:
    try:
        x0, y0, x1, y1 = (float(item) for item in value or ())
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def _optional_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _direction_rotation(value: object) -> int:
    try:
        x, y = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError):
        return 0
    if abs(x) >= abs(y):
        return 0 if x >= 0 else 180
    return 90 if y < 0 else 270


def _extract_vector_segments(drawings: Iterable[object]) -> tuple[LineSegment, ...]:
    """Extract straight line and rectangle edges; curves remain unclassified."""

    segments: list[LineSegment] = []
    for drawing in drawings:
        if not isinstance(drawing, dict):
            continue
        for item in drawing.get("items", ()):
            try:
                kind = item[0]
                if kind == "l":
                    segments.append(_line_segment(item[1], item[2]))
                elif kind == "re":
                    rectangle = item[1]
                    x0, y0, x1, y1 = (
                        float(rectangle.x0),
                        float(rectangle.y0),
                        float(rectangle.x1),
                        float(rectangle.y1),
                    )
                    segments.extend(
                        (
                            LineSegment((x0, y0), (x1, y0), GeometryEvidenceSource.VECTOR_PDF),
                            LineSegment((x1, y0), (x1, y1), GeometryEvidenceSource.VECTOR_PDF),
                            LineSegment((x1, y1), (x0, y1), GeometryEvidenceSource.VECTOR_PDF),
                            LineSegment((x0, y1), (x0, y0), GeometryEvidenceSource.VECTOR_PDF),
                        )
                    )
            except (AttributeError, IndexError, TypeError, ValueError):
                continue
    return tuple(segments)


def _line_segment(start: object, end: object) -> LineSegment:
    return LineSegment(
        (float(start.x), float(start.y)),
        (float(end.x), float(end.y)),
        GeometryEvidenceSource.VECTOR_PDF,
    )
