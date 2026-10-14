"""Compare two PDF revisions and annotate verified changes on a latest-copy PDF."""

from __future__ import annotations

import os
import math
import re
import uuid
from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter, ImageStat

from slimpdf.checker_models import (
    ChangeKind,
    ChangeRegion,
    CheckerRequest,
    CheckerResult,
    NormalizedRect,
    PageMatch,
)
from slimpdf.core.compression import publish_output_without_overwrite
from slimpdf.core.jobs import CancellationToken
from slimpdf.services.pdf_service import (
    SignedPdfNotSupportedError,
    _annotation_rect_matches,
    _page_geometry_matches,
    _render_all_output_pages,
    _snapshot_pdf_features,
    pdf_has_signatures,
)


CHECKER_DPI = 110
MAX_SCALE_DIFFERENCE = 0.08
PIXEL_THRESHOLD = 36
MIN_REGION_PIXELS = 24
REGION_MERGE_PROXIMITY = 8
CLOUD_MARK_MERGE_PROXIMITY = 8
REVISION_MARK_GROUP_PROXIMITY = 72.0
DRAWING_NUMBER_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9._/-]{4,}", re.IGNORECASE)
CheckerProgressCallback = Callable[[str, int, int], None]


class CheckerValidationError(ValueError):
    """Raised when inputs cannot be compared safely."""


def build_available_checked_path(latest_path: Path) -> Path:
    """Choose a latest_checked name without overwriting an existing result."""

    counter = 1
    while True:
        suffix = "_checked" if counter == 1 else f"_checked_{counter}"
        candidate = latest_path.with_name(f"{latest_path.stem}{suffix}.pdf")
        if not candidate.exists():
            return candidate
        counter += 1


def suggest_page_matches(
    previous_path: Path,
    latest_path: Path,
    *,
    progress: CheckerProgressCallback | None = None,
    cancellation: CancellationToken | None = None,
) -> tuple[PageMatch, ...]:
    """Greedily match pages using drawing-like tokens, text, and page geometry."""

    pymupdf = _load_pymupdf()
    token = cancellation or CancellationToken()
    with pymupdf.open(previous_path) as previous, pymupdf.open(latest_path) as latest:
        _require_open_documents(previous, latest)
        _report_progress(progress, "matching", 0, len(previous))
        candidates: list[tuple[float, int, int]] = []
        for old_index, old_page in enumerate(previous):
            token.raise_if_cancelled()
            old_text = old_page.get_text().strip()
            for new_index, new_page in enumerate(latest):
                score = _page_match_score(old_page, new_page, old_text, new_page.get_text().strip())
                candidates.append((score, old_index, new_index))
            _report_progress(progress, "matching", old_index + 1, len(previous))
        candidates.sort(reverse=True)
        used_old: set[int] = set()
        used_new: set[int] = set()
        matches: list[PageMatch] = []
        for score, old_index, new_index in candidates:
            if old_index in used_old or new_index in used_new:
                continue
            used_old.add(old_index)
            used_new.add(new_index)
            matches.append(PageMatch(old_index + 1, new_index + 1, round(score, 3)))
        return tuple(sorted(matches, key=lambda match: match.latest_page))


def compare_pdf_revisions(
    request: CheckerRequest,
    *,
    page_matches: tuple[PageMatch, ...] | None = None,
    cancellation: CancellationToken | None = None,
    progress: CheckerProgressCallback | None = None,
) -> CheckerResult:
    """Detect changes and publish a separately annotated copy of the latest PDF."""

    previous_path, latest_path, revision = _validate_request(request)
    token = cancellation or CancellationToken()
    if pdf_has_signatures(latest_path) and not request.allow_signature_invalidation:
        raise SignedPdfNotSupportedError(
            "최신 PDF에 전자서명이 있습니다. CHECK 주석 추가로 서명이 무효화될 수 있어 결과를 생성하지 않았습니다."
        )
    matches = page_matches or suggest_page_matches(
        previous_path,
        latest_path,
        progress=progress,
        cancellation=token,
    )
    if not matches:
        raise CheckerValidationError("자동으로 대응되는 페이지를 찾지 못했습니다.")
    pymupdf = _load_pymupdf()
    temporary = latest_path.parent / f".{latest_path.stem}.checker-{uuid.uuid4().hex}.tmp.pdf"
    previous = pymupdf.open(previous_path)
    latest = pymupdf.open(latest_path)
    changes: list[ChangeRegion] = []
    try:
        _require_open_documents(previous, latest)
        _validate_page_matches(matches, len(previous), len(latest))
        _report_progress(progress, "comparing", 0, len(matches))
        for completed_pages, match in enumerate(matches, start=1):
            token.raise_if_cancelled()
            old_page = previous[match.previous_page - 1]
            new_page = latest[match.latest_page - 1]
            _require_compatible_scale(old_page, new_page, match)
            old_image = _render_page(old_page)
            new_image, alignment_offset = _align_image_with_offset(
                old_image,
                _normalize_orientation_and_size(old_image, _render_page(new_page)),
            )
            regions = _detect_regions(
                old_image,
                new_image,
                request.exclusions,
                preserve_thin_changes=alignment_offset == (0, 0),
            )
            revision_marker_rectangles = []
            page_changes: list[ChangeRegion] = []
            for rectangle, kind in regions:
                pdf_rectangle = _pixel_to_pdf_rect(rectangle, old_image.size, new_page)
                change = ChangeRegion(match.latest_page, pdf_rectangle, kind)
                _add_change_cloud(new_page, change)
                page_changes.append(change)
            for marker_group in _group_revision_mark_changes(page_changes):
                anchor = _union_change_rectangles(marker_group)
                _add_revision_marker(
                    new_page,
                    anchor,
                    marker_group[0].kind,
                    revision,
                    revision_marker_rectangles,
                )
            changes.extend(page_changes)
            _report_progress(progress, "comparing", completed_pages, len(matches))
        token.raise_if_cancelled()
        _report_progress(progress, "saving", 0, 1)
        latest.save(temporary, garbage=3, deflate=True)
        latest.close()
        latest = None
        _report_progress(progress, "saving", 1, 1)
        _report_progress(progress, "validating", 0, 1)
        _validate_checker_output(latest_path, temporary, len(changes))
        _report_progress(progress, "validating", 1, 1)
        _report_progress(progress, "publishing", 0, 1)
        while True:
            output_path = build_available_checked_path(latest_path)
            try:
                publish_output_without_overwrite(temporary, output_path)
                break
            except FileExistsError:
                continue
        _report_progress(progress, "publishing", 1, 1)
        _report_progress(progress, "complete", 1, 1)
        return CheckerResult(previous_path, latest_path, output_path, matches, tuple(changes))
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        previous.close()
        if latest is not None:
            latest.close()


def _report_progress(
    callback: CheckerProgressCallback | None,
    stage: str,
    current: int,
    total: int,
) -> None:
    """Publish bounded progress without coupling core comparison to Tk."""

    if callback is not None:
        callback(stage, max(0, current), max(1, total))


def _validate_request(request: CheckerRequest) -> tuple[Path, Path, str]:
    previous = request.previous_path.expanduser().resolve()
    latest = request.latest_path.expanduser().resolve()
    if previous == latest:
        raise CheckerValidationError("서로 다른 Revision PDF 두 개를 선택하세요.")
    for path in (previous, latest):
        if not path.is_file() or path.suffix.casefold() != ".pdf":
            raise CheckerValidationError(f"PDF 파일을 찾을 수 없습니다: {path}")
        if path.stat().st_size <= 0:
            raise CheckerValidationError(f"빈 PDF는 비교할 수 없습니다: {path.name}")
    revision = request.revision_number.strip()
    if not revision or len(revision) > 32 or any(character in revision for character in "\r\n\t"):
        raise CheckerValidationError("Revision 번호를 1~32자로 입력하세요.")
    for exclusion in request.exclusions:
        if not (0 <= exclusion.x0 < exclusion.x1 <= 1 and 0 <= exclusion.y0 < exclusion.y1 <= 1):
            raise CheckerValidationError("제외 영역 좌표가 올바르지 않습니다.")
    return previous, latest, revision


def _require_open_documents(previous, latest) -> None:
    if previous.needs_pass or latest.needs_pass:
        raise CheckerValidationError("암호화 PDF는 먼저 승인된 보안 해제 절차를 완료하세요.")
    if len(previous) == 0 or len(latest) == 0:
        raise CheckerValidationError("페이지가 없는 PDF는 비교할 수 없습니다.")


def _page_match_score(old_page, new_page, old_text: str, new_text: str) -> float:
    old_tokens = set(DRAWING_NUMBER_PATTERN.findall(old_text.upper()))
    new_tokens = set(DRAWING_NUMBER_PATTERN.findall(new_text.upper()))
    token_score = 0.65 if old_tokens & new_tokens else 0.0
    old_words = set(old_text.casefold().split())
    new_words = set(new_text.casefold().split())
    text_score = 0.25 * len(old_words & new_words) / max(1, len(old_words | new_words))
    old_ratio = old_page.rect.width / max(1.0, old_page.rect.height)
    new_ratio = new_page.rect.width / max(1.0, new_page.rect.height)
    geometry_score = 0.10 * max(0.0, 1.0 - abs(old_ratio - new_ratio) / max(old_ratio, new_ratio))
    return token_score + text_score + geometry_score


def _validate_page_matches(matches: tuple[PageMatch, ...], previous_count: int, latest_count: int) -> None:
    old_pages = [item.previous_page for item in matches]
    new_pages = [item.latest_page for item in matches]
    if len(old_pages) != len(set(old_pages)) or len(new_pages) != len(set(new_pages)):
        raise CheckerValidationError("한 페이지를 여러 페이지에 중복 대응할 수 없습니다.")
    if any(page < 1 or page > previous_count for page in old_pages):
        raise CheckerValidationError("이전 Revision 페이지 대응값이 범위를 벗어났습니다.")
    if any(page < 1 or page > latest_count for page in new_pages):
        raise CheckerValidationError("최신 Revision 페이지 대응값이 범위를 벗어났습니다.")


def _require_compatible_scale(old_page, new_page, match: PageMatch) -> None:
    old_size = sorted((old_page.rect.width, old_page.rect.height))
    new_size = sorted((new_page.rect.width, new_page.rect.height))
    differences = [abs(old - new) / max(old, new) for old, new in zip(old_size, new_size, strict=True)]
    if max(differences) > MAX_SCALE_DIFFERENCE:
        raise CheckerValidationError(
            f"페이지 축척 차이가 너무 커 비교를 중단했습니다: 이전 {match.previous_page} / 최신 {match.latest_page}"
        )


def _render_page(page, target_size: tuple[int, int] | None = None) -> Image.Image:
    pymupdf = _load_pymupdf()
    scale = CHECKER_DPI / 72.0
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples).convert("L")
    if target_size and image.size != target_size:
        image = image.resize(target_size, Image.Resampling.BICUBIC)
    return image


def _normalize_orientation_and_size(reference: Image.Image, candidate: Image.Image) -> Image.Image:
    """Choose the right-angle orientation that best matches the previous revision."""

    variants = (
        candidate,
        candidate.rotate(90, expand=True, fillcolor=255),
        candidate.rotate(180, expand=True, fillcolor=255),
        candidate.rotate(270, expand=True, fillcolor=255),
    )
    sample_size = (max(80, reference.width // 8), max(80, reference.height // 8))
    reference_sample = reference.resize(sample_size, Image.Resampling.BILINEAR)
    scored = []
    for variant in variants:
        resized = variant.resize(reference.size, Image.Resampling.BICUBIC)
        sample = resized.resize(sample_size, Image.Resampling.BILINEAR)
        score = ImageStat.Stat(ImageChops.difference(reference_sample, sample)).mean[0]
        scored.append((score, resized))
    return min(scored, key=lambda item: item[0])[1]


def _align_image(reference: Image.Image, candidate: Image.Image) -> Image.Image:
    """Correct small scan-position offsets without altering the output PDF itself."""

    aligned, _offset = _align_image_with_offset(reference, candidate)
    return aligned


def _align_image_with_offset(
    reference: Image.Image,
    candidate: Image.Image,
) -> tuple[Image.Image, tuple[int, int]]:
    """Return an aligned image and the full-resolution translation that was used."""

    max_dimension = 420
    scale = min(1.0, max_dimension / max(reference.size))
    sample_size = (
        max(1, round(reference.width * scale)),
        max(1, round(reference.height * scale)),
    )
    old_sample = reference.resize(sample_size, Image.Resampling.BILINEAR)
    new_sample = candidate.resize(sample_size, Image.Resampling.BILINEAR)
    search_radius = max(2, round(12 * scale))
    best_score = float("inf")
    best_offset = (0, 0)
    for dy in range(-search_radius, search_radius + 1, 2):
        for dx in range(-search_radius, search_radius + 1, 2):
            shifted = new_sample.transform(
                sample_size,
                Image.Transform.AFFINE,
                (1, 0, -dx, 0, 1, -dy),
                fillcolor=255,
            )
            margin = search_radius + 2
            crop = (margin, margin, sample_size[0] - margin, sample_size[1] - margin)
            score = ImageStat.Stat(ImageChops.difference(old_sample.crop(crop), shifted.crop(crop))).mean[0]
            if score < best_score:
                best_score = score
                best_offset = (dx, dy)
    full_dx = round(best_offset[0] / scale)
    full_dy = round(best_offset[1] / scale)
    return (
        candidate.transform(
            candidate.size,
            Image.Transform.AFFINE,
            (1, 0, -full_dx, 0, 1, -full_dy),
            fillcolor=255,
        ),
        (full_dx, full_dy),
    )


def _detect_regions(
    old_image: Image.Image,
    new_image: Image.Image,
    exclusions: tuple[NormalizedRect, ...],
    *,
    preserve_thin_changes: bool = True,
) -> tuple[tuple[tuple[int, int, int, int], ChangeKind], ...]:
    difference = ImageChops.difference(old_image, new_image)
    # Expand thin strokes on already aligned vector drawings. A median filter
    # erased small dates, dimensions, and equipment labels. For translated scan
    # pages, retain median denoising so antialiased shift edges are not reported.
    difference_filter = (
        ImageFilter.MaxFilter(3)
        if preserve_thin_changes
        else ImageFilter.MedianFilter(3)
    )
    difference = difference.filter(difference_filter)
    mask = difference.point(lambda value: 255 if value >= PIXEL_THRESHOLD else 0, mode="1")
    pixels = mask.load()
    width, height = mask.size
    for exclusion in exclusions:
        x0, y0 = int(exclusion.x0 * width), int(exclusion.y0 * height)
        x1, y1 = int(exclusion.x1 * width), int(exclusion.y1 * height)
        for y in range(max(0, y0), min(height, y1)):
            for x in range(max(0, x0), min(width, x1)):
                pixels[x, y] = 0
    regions = _merge_regions(
        _connected_regions(mask),
        proximity=REGION_MERGE_PROXIMITY,
    )
    # Each detection box is expanded before annotation. Merge those expanded
    # boxes when the cloud scallops would overlap or nearly touch, so one
    # practical change area receives one cloud and one revision marker.
    cloud_regions = _merge_regions(
        [_expand_rectangle(rectangle, width, height, 5) for rectangle in regions],
        proximity=CLOUD_MARK_MERGE_PROXIMITY,
    )
    classified = []
    for cloud_rectangle in cloud_regions:
        old_dark = _dark_ratio(old_image.crop(cloud_rectangle))
        new_dark = _dark_ratio(new_image.crop(cloud_rectangle))
        if new_dark > old_dark * 1.35 + 0.005:
            kind = ChangeKind.ADDED_OR_MODIFIED
        elif old_dark > new_dark * 1.35 + 0.005:
            kind = ChangeKind.DELETED
        else:
            old_crop = old_image.crop(cloud_rectangle)
            new_crop = new_image.crop(cloud_rectangle)
            kind = (
                ChangeKind.MOVED_OR_RESHAPED
                if _dark_centroid_shift(old_crop, new_crop) >= 0.06
                else ChangeKind.ADDED_OR_MODIFIED
            )
        classified.append((cloud_rectangle, kind))
    return tuple(classified)


def _merge_regions(
    regions: list[tuple[int, int, int, int]], *, proximity: int
) -> list[tuple[int, int, int, int]]:
    """Merge character-level boxes into practical drawing change areas."""

    pending = list(regions)
    merged: list[tuple[int, int, int, int]] = []
    while pending:
        current = pending.pop(0)
        changed = True
        while changed:
            changed = False
            remaining = []
            for candidate in pending:
                if _rectangles_are_near(current, candidate, proximity):
                    current = (
                        min(current[0], candidate[0]),
                        min(current[1], candidate[1]),
                        max(current[2], candidate[2]),
                        max(current[3], candidate[3]),
                    )
                    changed = True
                else:
                    remaining.append(candidate)
            pending = remaining
        merged.append(current)
    merged.sort(key=lambda item: (item[1], item[0]))
    return merged


def _rectangles_are_near(first, second, proximity: int) -> bool:
    return not (
        first[2] + proximity < second[0]
        or second[2] + proximity < first[0]
        or first[3] + proximity < second[1]
        or second[3] + proximity < first[1]
    )


def _connected_regions(mask: Image.Image) -> list[tuple[int, int, int, int]]:
    width, height = mask.size
    pixels = mask.load()
    visited: set[tuple[int, int]] = set()
    regions: list[tuple[int, int, int, int]] = []
    step = 2
    for y in range(0, height, step):
        for x in range(0, width, step):
            if not pixels[x, y] or (x, y) in visited:
                continue
            stack = [(x, y)]
            visited.add((x, y))
            points: list[tuple[int, int]] = []
            while stack:
                current_x, current_y = stack.pop()
                points.append((current_x, current_y))
                for next_x, next_y in (
                    (current_x - step, current_y), (current_x + step, current_y),
                    (current_x, current_y - step), (current_x, current_y + step),
                ):
                    if 0 <= next_x < width and 0 <= next_y < height and pixels[next_x, next_y] and (next_x, next_y) not in visited:
                        visited.add((next_x, next_y))
                        stack.append((next_x, next_y))
            if len(points) * step * step >= MIN_REGION_PIXELS:
                xs, ys = zip(*points, strict=True)
                regions.append((min(xs), min(ys), max(xs) + step, max(ys) + step))
    regions.sort(key=lambda item: (item[1], item[0]))
    return regions


def _expand_rectangle(rectangle, width: int, height: int, margin: int):
    x0, y0, x1, y1 = rectangle
    return max(0, x0 - margin), max(0, y0 - margin), min(width, x1 + margin), min(height, y1 + margin)


def _dark_ratio(image: Image.Image) -> float:
    histogram = image.histogram()
    dark = sum(histogram[:180])
    return dark / max(1, image.width * image.height)


def _dark_centroid_shift(old_image: Image.Image, new_image: Image.Image) -> float:
    """Return normalized ink-centroid movement between two aligned crops.

    Similar amounts of ink usually indicate either a replacement at the same
    location or content that physically moved.  Distinguishing those cases by
    centroid movement prevents ordinary text / value edits from being
    classified as movement merely because their dark-pixel counts are similar.
    """

    def centroid(image: Image.Image) -> tuple[float, float] | None:
        grayscale = image.convert("L")
        pixels = grayscale.load()
        total_weight = weighted_x = weighted_y = 0.0
        for y in range(grayscale.height):
            for x in range(grayscale.width):
                value = pixels[x, y]
                if value >= 180:
                    continue
                weight = 180 - value
                total_weight += weight
                weighted_x += x * weight
                weighted_y += y * weight
        if total_weight == 0:
            return None
        return weighted_x / total_weight, weighted_y / total_weight

    old_centroid = centroid(old_image)
    new_centroid = centroid(new_image)
    if old_centroid is None or new_centroid is None:
        return 0.0
    dx = (new_centroid[0] - old_centroid[0]) / max(1, old_image.width)
    dy = (new_centroid[1] - old_centroid[1]) / max(1, old_image.height)
    return (dx * dx + dy * dy) ** 0.5


def _pixel_to_pdf_rect(rectangle, image_size, page):
    x0, y0, x1, y1 = rectangle
    width, height = image_size
    pymupdf = _load_pymupdf()
    display_rect = pymupdf.Rect(
        page.rect.x0 + x0 / width * page.rect.width,
        page.rect.y0 + y0 / height * page.rect.height,
        page.rect.x0 + x1 / width * page.rect.width,
        page.rect.y0 + y1 / height * page.rect.height,
    )
    unrotated_rect = display_rect * page.derotation_matrix
    return tuple(unrotated_rect)


def _add_change_annotation(
    page,
    change: ChangeRegion,
    revision: str,
    revision_marker_rectangles: list | None = None,
) -> None:
    """Add one cloud and its own revision mark (compatibility helper)."""

    _add_change_cloud(page, change)
    _add_revision_marker(
        page,
        change.rectangle,
        change.kind,
        revision,
        revision_marker_rectangles,
    )


def _change_color(kind: ChangeKind) -> tuple[float, float, float]:
    """Map changes to the two user-facing revision color families."""

    if kind is ChangeKind.ADDED_OR_MODIFIED:
        return (1.0, 0.0, 0.0)
    return (0.0, 0.35, 1.0)


def _add_change_cloud(page, change: ChangeRegion) -> None:
    """Add a cloud annotation without creating a revision marker."""

    pymupdf = _load_pymupdf()
    labels = {
        ChangeKind.ADDED_OR_MODIFIED: "추가/수정",
        ChangeKind.DELETED: "삭제",
        ChangeKind.MOVED_OR_RESHAPED: "이동/형상 변경",
    }
    rectangle = pymupdf.Rect(change.rectangle)
    annotation = page.add_rect_annot(rectangle)
    annotation.set_colors(stroke=_change_color(change.kind))
    annotation.set_border(width=0.5, clouds=1)
    annotation.set_info(
        title="SlimPDF CHECKER",
        subject=labels[change.kind],
        content=f"Change: {labels[change.kind]}",
    )
    annotation.update(opacity=0.95)


def _add_revision_marker(
    page,
    anchor_rectangle,
    kind: ChangeKind,
    revision: str,
    revision_marker_rectangles: list | None = None,
) -> None:
    """Add one revision triangle for a nearby same-family change group."""

    pymupdf = _load_pymupdf()
    rectangle = pymupdf.Rect(anchor_rectangle)
    marker_text = revision
    text_rotation = _nearby_text_rotation(page, rectangle)
    marker_scale = 0.8
    base_size = max(16.0, min(30.0, len(marker_text) * 4.2 + 12.0))
    base_height = max(14.0, base_size * 0.78)
    size = base_size * marker_scale
    height = base_height * marker_scale
    occupied_markers = revision_marker_rectangles if revision_marker_rectangles is not None else []
    marker_width, marker_height = (
        (height, size) if text_rotation in (90, 270) else (size, height)
    )
    marker_rect = _find_revision_marker_rect(
        page,
        rectangle,
        marker_width,
        marker_height,
        occupied_markers,
    )
    triangle = _rotated_triangle_points(marker_rect, size, height, text_rotation)
    triangle_annotation = page.add_polygon_annot(
        triangle,
    )
    triangle_annotation.set_colors(stroke=_change_color(kind))
    triangle_annotation.set_border(width=0.5)
    triangle_annotation.set_info(
        title="SlimPDF CHECKER",
        subject="REVISION MARK",
        content="Revision mark triangle",
    )
    triangle_annotation.update(opacity=0.95)
    font_size = max(7.0, min(11.0, base_height * 0.55)) * marker_scale
    text_rect = _revision_text_rect(
        marker_rect,
        size,
        height,
        text_rotation,
        marker_scale,
    )
    number_annotation = page.add_freetext_annot(
        text_rect,
        marker_text,
        fontsize=font_size,
        fontname="Arial",
        text_color=(0.0, 0.0, 0.0),
        align=1,
        rotate=text_rotation,
    )
    number_annotation.set_info(
        title="SlimPDF CHECKER",
        subject="REVISION MARK NUMBER",
    )
    number_annotation.update(opacity=0.95)
    _use_embedded_arial_for_freetext(page, number_annotation)
    occupied_markers.append(marker_rect)


def _revision_mark_family(kind: ChangeKind) -> str:
    return "red" if kind is ChangeKind.ADDED_OR_MODIFIED else "blue"


def _group_revision_mark_changes(
    changes: list[ChangeRegion],
) -> tuple[tuple[ChangeRegion, ...], ...]:
    """Group nearby changes only when they share the same color family."""

    pending = list(changes)
    groups: list[tuple[ChangeRegion, ...]] = []
    while pending:
        group = [pending.pop(0)]
        group_rect = group[0].rectangle
        family = _revision_mark_family(group[0].kind)
        changed = True
        while changed:
            changed = False
            remaining = []
            for candidate in pending:
                if (
                    _revision_mark_family(candidate.kind) == family
                    and _rectangles_are_near(
                        group_rect,
                        candidate.rectangle,
                        REVISION_MARK_GROUP_PROXIMITY,
                    )
                ):
                    group.append(candidate)
                    group_rect = _union_change_rectangles(group)
                    changed = True
                else:
                    remaining.append(candidate)
            pending = remaining
        groups.append(tuple(group))
    return tuple(groups)


def _union_change_rectangles(changes) -> tuple[float, float, float, float]:
    rectangles = [change.rectangle for change in changes]
    return (
        min(rectangle[0] for rectangle in rectangles),
        min(rectangle[1] for rectangle in rectangles),
        max(rectangle[2] for rectangle in rectangles),
        max(rectangle[3] for rectangle in rectangles),
    )


def _nearby_text_rotation(page, change_rect) -> int:
    """Return the cardinal direction of text nearest to a changed region."""

    pymupdf = _load_pymupdf()
    change = pymupdf.Rect(change_rect)
    center = pymupdf.Point(
        (change.x0 + change.x1) / 2,
        (change.y0 + change.y1) / 2,
    )
    candidates: list[tuple[float, int, int]] = []
    dominant_weights: dict[int, int] = {}
    for block in page.get_text("dict").get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            direction = line.get("dir")
            bbox = line.get("bbox")
            if not direction or len(direction) != 2 or not bbox:
                continue
            characters = sum(
                len(str(span.get("text", "")).strip())
                for span in line.get("spans", ())
            )
            if characters <= 0:
                continue
            angle = math.degrees(
                math.atan2(-float(direction[1]), float(direction[0]))
            )
            rotation = int(round(angle / 90.0) * 90) % 360
            dominant_weights[rotation] = dominant_weights.get(rotation, 0) + characters
            line_rect = pymupdf.Rect(bbox)
            line_center = pymupdf.Point(
                (line_rect.x0 + line_rect.x1) / 2,
                (line_rect.y0 + line_rect.y1) / 2,
            )
            distance = math.hypot(line_center.x - center.x, line_center.y - center.y)
            if not (line_rect & change).is_empty:
                distance = 0.0
            candidates.append((distance, -characters, rotation))
    if candidates:
        nearest = min(candidates)
        maximum_nearby_distance = max(change.width, change.height) * 2.5 + 36.0
        if nearest[0] <= maximum_nearby_distance:
            return nearest[2]
    if dominant_weights:
        return max(dominant_weights.items(), key=lambda item: item[1])[0]
    return 0


def _rotated_triangle_points(marker_rect, width: float, height: float, rotation: int):
    """Build a triangle whose apex and number follow the selected text direction."""

    pymupdf = _load_pymupdf()
    center_x = (marker_rect.x0 + marker_rect.x1) / 2
    center_y = (marker_rect.y0 + marker_rect.y1) / 2
    radians = math.radians(rotation)
    cosine, sine = math.cos(radians), math.sin(radians)

    def rotate(x: float, y: float):
        return pymupdf.Point(
            center_x + x * cosine + y * sine,
            center_y - x * sine + y * cosine,
        )

    lower_left = rotate(-width / 2, height / 2)
    lower_right = rotate(width / 2, height / 2)
    apex = rotate(0, -height / 2)
    return [lower_left, lower_right, apex, lower_left]


def _revision_text_rect(
    marker_rect,
    width: float,
    height: float,
    rotation: int,
    marker_scale: float,
):
    """Build a rotated FreeText box with optical vertical centering.

    FreeText ``align=1`` centers only horizontally.  Its first baseline starts
    at the top of the annotation rectangle, so a symmetric inset places the
    glyph too close to the triangle apex.  The canonical box is shifted toward
    the triangle base before it is rotated with the complete revision mark.
    """

    pymupdf = _load_pymupdf()
    center_x = (marker_rect.x0 + marker_rect.x1) / 2
    center_y = (marker_rect.y0 + marker_rect.y1) / 2
    radians = math.radians(rotation)
    cosine, sine = math.cos(radians), math.sin(radians)
    horizontal_inset = 1.5 * marker_scale
    base_inset = 1.0 * marker_scale
    local_corners = (
        (-width / 2 + horizontal_inset, -height / 2 + height * 0.34),
        (width / 2 - horizontal_inset, -height / 2 + height * 0.34),
        (width / 2 - horizontal_inset, height / 2 - base_inset),
        (-width / 2 + horizontal_inset, height / 2 - base_inset),
    )
    rotated = [
        (
            center_x + x * cosine + y * sine,
            center_y - x * sine + y * cosine,
        )
        for x, y in local_corners
    ]
    xs, ys = zip(*rotated, strict=True)
    return pymupdf.Rect(min(xs), min(ys), max(xs), max(ys))


def _use_embedded_arial_for_freetext(page, annotation) -> None:
    """Replace a FreeText appearance font with an embedded Windows Arial font."""

    windows_directory = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        windows_directory / "Fonts" / "arial.ttf",
        windows_directory / "Fonts" / "ARIAL.TTF",
    )
    font_path = next((path for path in candidates if path.is_file()), None)
    if font_path is None:
        raise CheckerValidationError(
            "REVISION MARK에 필요한 Arial 글꼴을 Windows Fonts 폴더에서 찾지 못했습니다."
        )
    document = page.parent
    font_xref = page.insert_font(
        fontname="ArialRevisionMark",
        fontfile=str(font_path),
        set_simple=True,
    )
    appearance_type, appearance_value = document.xref_get_key(annotation.xref, "AP")
    match = re.search(r"/N\s+(\d+)\s+0\s+R", appearance_value)
    if appearance_type != "dict" or match is None:
        raise CheckerValidationError(
            "REVISION MARK 번호 주석의 Arial appearance를 생성하지 못했습니다."
        )
    appearance_xref = int(match.group(1))
    document.xref_set_key(
        appearance_xref,
        "Resources/Font/Helv",
        f"{font_xref} 0 R",
    )


def _find_revision_marker_rect(
    page,
    cloud_rect,
    width: float,
    height: float,
    occupied_markers: list,
):
    """Choose the nearest low-ink area around a cloud for its revision mark."""

    pymupdf = _load_pymupdf()
    bounds = page.mediabox
    center_x = (cloud_rect.x0 + cloud_rect.x1) / 2
    center_y = (cloud_rect.y0 + cloud_rect.y1) / 2
    candidates = []
    for gap in (4.0, 12.0, 24.0, 36.0):
        candidates.extend(
            (
                pymupdf.Rect(cloud_rect.x1 + gap, center_y - height / 2, cloud_rect.x1 + gap + width, center_y + height / 2),
                pymupdf.Rect(cloud_rect.x0 - gap - width, center_y - height / 2, cloud_rect.x0 - gap, center_y + height / 2),
                pymupdf.Rect(center_x - width / 2, cloud_rect.y0 - gap - height, center_x + width / 2, cloud_rect.y0 - gap),
                pymupdf.Rect(center_x - width / 2, cloud_rect.y1 + gap, center_x + width / 2, cloud_rect.y1 + gap + height),
                pymupdf.Rect(cloud_rect.x1 + gap, cloud_rect.y0 - gap - height, cloud_rect.x1 + gap + width, cloud_rect.y0 - gap),
                pymupdf.Rect(cloud_rect.x1 + gap, cloud_rect.y1 + gap, cloud_rect.x1 + gap + width, cloud_rect.y1 + gap + height),
                pymupdf.Rect(cloud_rect.x0 - gap - width, cloud_rect.y0 - gap - height, cloud_rect.x0 - gap, cloud_rect.y0 - gap),
                pymupdf.Rect(cloud_rect.x0 - gap - width, cloud_rect.y1 + gap, cloud_rect.x0 - gap, cloud_rect.y1 + gap + height),
            )
        )
    valid = [
        (candidate, index)
        for index, candidate in enumerate(candidates)
        if bounds.contains(candidate)
    ]
    if not valid:
        gap = 4.0
        fallback_x0 = max(
            bounds.x0 + 2,
            min(bounds.x1 - width - 2, cloud_rect.x1 + gap),
        )
        fallback_y0 = max(
            bounds.y0 + 2,
            min(bounds.y1 - height - 2, cloud_rect.y0),
        )
        return pymupdf.Rect(
            fallback_x0,
            fallback_y0,
            fallback_x0 + width,
            fallback_y0 + height,
        )

    annotation_rectangles = [
        pymupdf.Rect(item.rect)
        for item in (page.annots() or ())
    ]

    def score(item):
        candidate, order = item
        collisions = sum(
            1
            for occupied in (*annotation_rectangles, *occupied_markers)
            if not (candidate & occupied).is_empty
        )
        return collisions, _page_region_dark_ratio(page, candidate), order

    return min(valid, key=score)[0]


def _page_region_dark_ratio(page, rectangle) -> float:
    """Estimate how much visible content occupies one candidate marker area."""

    pymupdf = _load_pymupdf()
    pixmap = page.get_pixmap(
        matrix=pymupdf.Matrix(1, 1),
        colorspace=pymupdf.csGRAY,
        alpha=False,
        annots=False,
        clip=rectangle,
    )
    samples = pixmap.samples
    return sum(value < 245 for value in samples) / max(1, len(samples))


def _validate_checker_output(input_path: Path, output_path: Path, expected_changes: int) -> None:
    if not output_path.is_file() or output_path.stat().st_size <= 0:
        raise CheckerValidationError("CHECK 결과 PDF가 생성되지 않았습니다.")
    pikepdf = _load_pikepdf()
    with pikepdf.open(input_path) as source, pikepdf.open(output_path) as output:
        source_features = _snapshot_pdf_features(source)
        output_features = _snapshot_pdf_features(output)
        if len(source.pages) != len(output.pages):
            raise CheckerValidationError("CHECK 전후 페이지 수가 다릅니다.")
        if not _page_geometry_matches(source_features.page_geometry, output_features.page_geometry):
            raise CheckerValidationError("CHECK 과정에서 페이지 크기 또는 회전이 변경됐습니다.")
        if source_features.form_fields != output_features.form_fields:
            raise CheckerValidationError("CHECK 과정에서 PDF 폼이 변경됐습니다.")
        if source_features.attachments != output_features.attachments:
            raise CheckerValidationError("CHECK 과정에서 첨부파일이 변경됐습니다.")
        if not _original_annotations_preserved(
            source_features.annotations, output_features.annotations
        ):
            raise CheckerValidationError("CHECK 과정에서 기존 PDF 주석이 변경됐습니다.")
        source_annotation_count = sum(len(page) for page in source_features.annotations)
        output_annotation_count = sum(len(page) for page in output_features.annotations)
        if output_annotation_count < source_annotation_count + expected_changes:
            raise CheckerValidationError("CHECK 변경 주석이 결과 PDF에 모두 기록되지 않았습니다.")
    _render_all_output_pages(output_path)


def _original_annotations_preserved(source_pages, output_pages) -> bool:
    """Require every original annotation while permitting new Checker annotations."""

    if len(source_pages) != len(output_pages):
        return False
    for source_page, output_page in zip(source_pages, output_pages, strict=True):
        candidates = [dict(item) for item in output_page]
        for source_item in source_page:
            expected = dict(source_item)
            expected_rect = expected.pop("/Rect", None)
            found_index = None
            for index, candidate in enumerate(candidates):
                candidate_values = dict(candidate)
                candidate_rect = candidate_values.pop("/Rect", None)
                if expected != candidate_values:
                    continue
                if expected_rect is not None and not _annotation_rect_matches(
                    expected_rect, candidate_rect
                ):
                    continue
                found_index = index
                break
            if found_index is None:
                return False
            candidates.pop(found_index)
    return True


def _load_pymupdf():
    try:
        import pymupdf
    except ImportError as error:
        raise RuntimeError("PyMuPDF가 설치되어 있지 않습니다.") from error
    return pymupdf


def _load_pikepdf():
    try:
        import pikepdf
    except ImportError as error:
        raise RuntimeError("pikepdf가 설치되어 있지 않습니다.") from error
    return pikepdf
