"""Low-confidence cascade for engineering text, preserving source coordinates."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from slimpdf.core.jobs import CancellationToken
from slimpdf.ocr_models import OcrTextResult, OcrWord
from slimpdf.services.tesseract_service import recognize_image, recognize_image_with_psm
from slimpdf.utils.temp_paths import managed_temporary_directory


Recognizer = Callable[[Path, tuple[str, ...]], OcrTextResult]
PsmRecognizer = Callable[[Path, tuple[str, ...], int], OcrTextResult]
PASS_CONFIDENCE = 78.0
PASS_MINIMUM_WORDS = 8
MAX_PREPARED_LONG_EDGE = 7000
TILE_SIZE = 3072
TILE_OVERLAP_RATIO = 0.15
TILE_TRIGGER_LONG_EDGE = 4096
ENGINEERING_PATTERN = re.compile(
    r"(?:\b[A-Z]{1,6}[-_]?[A-Z0-9]{1,8}\b|\bDN\s*\d+\b|[Ø⌀]\s*\d+(?:\.\d+)?|"
    r"[-+]?\d+(?:\.\d+)?\s*(?:mm|bar|kPa|MPa|°?C|V|Hz)\b)",
    re.IGNORECASE,
)
LOGGER = logging.getLogger(__name__)


def recognize_engineering_image(
    image_path: Path,
    languages: tuple[str, ...] = ("kor", "eng"),
    *,
    recognizer: Recognizer = recognize_image,
    psm_recognizer: PsmRecognizer | None = None,
    cancellation: CancellationToken | None = None,
) -> OcrTextResult:
    """Use fast-path OCR, then retry only uncertain engineering images."""

    token = cancellation or CancellationToken()
    call_id = uuid4().hex[:12]
    if psm_recognizer is None and recognizer is recognize_image:
        psm_recognizer = _recognize_with_psm
    with Image.open(image_path) as opened:
        source = opened.convert("RGB")
    LOGGER.debug(
        "Engineering OCR cascade started: call_id=%s width=%d height=%d tiled=%s",
        call_id,
        source.width,
        source.height,
        max(source.size) > TILE_TRIGGER_LONG_EDGE,
    )
    with managed_temporary_directory("slimpdf-ocr-") as workspace:
        if max(source.size) > TILE_TRIGGER_LONG_EDGE:
            result = _recognize_tiled(
                source,
                languages,
                recognizer,
                psm_recognizer,
                token,
                workspace,
                call_id,
            )
            LOGGER.debug(
                "Engineering OCR cascade completed: call_id=%s mode=tiled "
                "words=%d average_confidence=%.2f",
                call_id,
                len(result.words),
                result.average_confidence,
            )
            return result
        return _recognize_single(
            image_path,
            source,
            languages,
            recognizer,
            psm_recognizer,
            token,
            workspace,
            diagnostic_id=call_id,
        )


def _recognize_single(
    image_path: Path,
    source: Image.Image,
    languages: tuple[str, ...],
    recognizer: Recognizer,
    psm_recognizer: PsmRecognizer | None,
    token: CancellationToken,
    workspace: Path,
    *,
    file_stem: str = "page",
    diagnostic_id: str = "internal",
) -> OcrTextResult:
    """Run the adaptive OCR cascade for one image or tile."""

    token.raise_if_cancelled()
    primary = recognizer(image_path, languages)
    if _passes_fast_path(primary):
        _log_selection(diagnostic_id, file_stem, "primary", (("primary", primary),))
        return primary

    prepared, scale = _prepare(source)
    prepared_path = workspace / f"{file_stem}-contrast-upscaled.png"
    prepared.save(prepared_path, format="PNG")
    enhanced = _scale_to_source(recognizer(prepared_path, languages), scale)
    candidates = [("primary", primary), ("contrast_upscaled", enhanced)]
    if not _passes_fast_path(enhanced) and psm_recognizer is not None:
        threshold_path = workspace / f"{file_stem}-threshold.png"
        _threshold(prepared).save(threshold_path, format="PNG")
        for candidate_path, psm in (
            (prepared_path, 6),
            (prepared_path, 11),
            (threshold_path, 11),
        ):
            token.raise_if_cancelled()
            psm_candidate = _scale_to_source(
                psm_recognizer(candidate_path, languages, psm), scale
            )
            candidates.append((f"psm_{psm}" if candidate_path == prepared_path else f"threshold_psm_{psm}", psm_candidate))
            if _passes_fast_path(psm_candidate):
                break

    best = max(candidates, key=lambda item: _candidate_score(item[1]))[1]
    if not _passes_fast_path(best):
        for angle in (90, 180, 270):
            token.raise_if_cancelled()
            rotated = prepared.rotate(angle, expand=True, fillcolor="white")
            rotated_path = workspace / f"{file_stem}-rotated-{angle}.png"
            rotated.save(rotated_path, format="PNG")
            result = recognizer(rotated_path, languages)
            unrotated = _restore_rotated_result(
                result, prepared.width, prepared.height, angle
            )
            candidates.append(
                (f"rotation_{angle}", _scale_to_source(unrotated, scale))
            )
    selected_name, selected = max(
        candidates, key=lambda item: _candidate_score(item[1])
    )
    _log_selection(diagnostic_id, file_stem, selected_name, tuple(candidates))
    return selected


def _recognize_tiled(
    source: Image.Image,
    languages: tuple[str, ...],
    recognizer: Recognizer,
    psm_recognizer: PsmRecognizer | None,
    token: CancellationToken,
    workspace: Path,
    diagnostic_id: str,
) -> OcrTextResult:
    """OCR a large drawing in overlapping tiles and restore page coordinates."""

    overlap = max(1, round(TILE_SIZE * TILE_OVERLAP_RATIO))
    x_origins = _tile_origins(source.width, TILE_SIZE, overlap)
    y_origins = _tile_origins(source.height, TILE_SIZE, overlap)
    translated_results: list[OcrTextResult] = []
    for row, top in enumerate(y_origins):
        for column, left in enumerate(x_origins):
            token.raise_if_cancelled()
            right = min(source.width, left + TILE_SIZE)
            bottom = min(source.height, top + TILE_SIZE)
            tile = source.crop((left, top, right, bottom))
            stem = f"tile-{row}-{column}"
            tile_path = workspace / f"{stem}.png"
            tile.save(tile_path, format="PNG")
            result = _recognize_single(
                tile_path,
                tile,
                languages,
                recognizer,
                psm_recognizer,
                token,
                workspace,
                file_stem=stem,
                diagnostic_id=f"{diagnostic_id}:{stem}",
            )
            translated_results.append(_translate_result(result, left, top))
    return _merge_tile_results(translated_results)


def _recognize_with_psm(
    image_path: Path, languages: tuple[str, ...], psm: int
) -> OcrTextResult:
    return recognize_image_with_psm(image_path, languages, psm=psm)


def _tile_origins(length: int, tile_size: int, overlap: int) -> tuple[int, ...]:
    """Return origins that cover an axis completely, including its final edge."""

    if length <= tile_size:
        return (0,)
    step = tile_size - overlap
    if step <= 0:
        raise ValueError("Tile overlap must be smaller than tile size.")
    origins = list(range(0, length - tile_size + 1, step))
    final_origin = length - tile_size
    if origins[-1] != final_origin:
        origins.append(final_origin)
    return tuple(origins)


def _translate_result(result: OcrTextResult, left: int, top: int) -> OcrTextResult:
    return OcrTextResult(
        tuple(
            OcrWord(
                word.text,
                word.confidence,
                word.left + left,
                word.top + top,
                word.width,
                word.height,
            )
            for word in result.words
        ),
        result.text,
        result.average_confidence,
    )


def _merge_tile_results(results: list[OcrTextResult]) -> OcrTextResult:
    """Merge identical words only when their source-coordinate boxes overlap."""

    retained: list[OcrWord] = []
    for result in results:
        for candidate in result.words:
            duplicate_index = next(
                (
                    index
                    for index, existing in enumerate(retained)
                    if _same_word(candidate, existing)
                ),
                None,
            )
            if duplicate_index is None:
                retained.append(candidate)
            elif candidate.confidence > retained[duplicate_index].confidence:
                retained[duplicate_index] = candidate
    retained.sort(key=lambda word: (word.top, word.left))
    text = _words_to_text(retained)
    average = (
        sum(word.confidence for word in retained) / len(retained) if retained else 0.0
    )
    return OcrTextResult(tuple(retained), text, average)


def _same_word(first: OcrWord, second: OcrWord) -> bool:
    if _normalise_word(first.text) != _normalise_word(second.text):
        return False
    intersection_width = max(
        0,
        min(first.left + first.width, second.left + second.width)
        - max(first.left, second.left),
    )
    intersection_height = max(
        0,
        min(first.top + first.height, second.top + second.height)
        - max(first.top, second.top),
    )
    intersection = intersection_width * intersection_height
    smaller_area = min(first.width * first.height, second.width * second.height)
    return smaller_area > 0 and intersection / smaller_area >= 0.45


def _normalise_word(text: str) -> str:
    return "".join(text.casefold().split())


def _words_to_text(words: list[OcrWord]) -> str:
    if not words:
        return ""
    lines: list[list[OcrWord]] = []
    for word in words:
        center = word.top + word.height / 2
        line = next(
            (
                current
                for current in reversed(lines)
                if abs(
                    center
                    - sum(item.top + item.height / 2 for item in current)
                    / len(current)
                )
                <= max(word.height, max(item.height for item in current)) * 0.65
            ),
            None,
        )
        if line is None:
            lines.append([word])
        else:
            line.append(word)
    return "\n".join(
        " ".join(item.text for item in sorted(line, key=lambda item: item.left))
        for line in lines
    )


def _passes_fast_path(result: OcrTextResult) -> bool:
    return (
        len(result.words) >= PASS_MINIMUM_WORDS
        and result.average_confidence >= PASS_CONFIDENCE
    )


def _prepare(image: Image.Image) -> tuple[Image.Image, float]:
    grayscale = ImageOps.autocontrast(ImageOps.grayscale(image), cutoff=(0.5, 0.5))
    grayscale = ImageEnhance.Contrast(grayscale).enhance(1.35)
    grayscale = grayscale.filter(
        ImageFilter.UnsharpMask(radius=1.1, percent=140, threshold=2)
    )
    long_edge = max(grayscale.size)
    scale = min(2.0, MAX_PREPARED_LONG_EDGE / max(1, long_edge))
    scale = max(1.0, scale)
    if scale > 1.01:
        grayscale = grayscale.resize(
            (round(grayscale.width * scale), round(grayscale.height * scale)),
            Image.Resampling.LANCZOS,
        )
    return grayscale, scale


def _threshold(image: Image.Image) -> Image.Image:
    """Create a conservative binary candidate without changing its dimensions."""

    histogram = image.histogram()
    total = sum(histogram)
    weighted_sum = sum(level * count for level, count in enumerate(histogram))
    threshold = max(110, min(210, round(weighted_sum / max(1, total) * 0.82)))
    return image.point(lambda value: 255 if value >= threshold else 0, mode="1")


def _candidate_score(result: OcrTextResult) -> tuple[float, int, int]:
    pattern_matches = len(ENGINEERING_PATTERN.findall(result.text))
    # Pattern agreement is evidence, but must not override a major confidence loss.
    score = result.average_confidence + min(8.0, pattern_matches * 1.5)
    return score, pattern_matches, len(result.words)


def _log_selection(
    diagnostic_id: str,
    region: str,
    selected_name: str,
    candidates: tuple[tuple[str, OcrTextResult], ...],
) -> None:
    """Log real candidate metadata without retaining recognized text."""

    summary = ",".join(
        f"{name}:{result.average_confidence:.2f}/{len(result.words)}/"
        f"{_candidate_score(result)[0]:.2f}"
        for name, result in candidates
    )
    LOGGER.debug(
        "Engineering OCR candidate selected: call_id=%s region=%s selected=%s "
        "candidates=confidence/words/score[%s]",
        diagnostic_id,
        region,
        selected_name,
        summary,
    )


def _scale_to_source(result: OcrTextResult, scale: float) -> OcrTextResult:
    if scale <= 1.01:
        return result
    return OcrTextResult(
        tuple(
            OcrWord(
                word.text,
                word.confidence,
                round(word.left / scale),
                round(word.top / scale),
                max(1, round(word.width / scale)),
                max(1, round(word.height / scale)),
            )
            for word in result.words
        ),
        result.text,
        result.average_confidence,
    )


def _restore_rotated_result(
    result: OcrTextResult, width: int, height: int, angle: int
) -> OcrTextResult:
    """Map PIL counter-clockwise rotation boxes back to prepared-image pixels."""

    restored: list[OcrWord] = []
    for word in result.words:
        if angle == 90:
            left = width - (word.top + word.height)
            top = word.left
            box_width, box_height = word.height, word.width
        elif angle == 180:
            left = width - (word.left + word.width)
            top = height - (word.top + word.height)
            box_width, box_height = word.width, word.height
        elif angle == 270:
            left = word.top
            top = height - (word.left + word.width)
            box_width, box_height = word.height, word.width
        else:
            left, top = word.left, word.top
            box_width, box_height = word.width, word.height
        restored.append(
            OcrWord(
                word.text,
                word.confidence,
                max(0, left),
                max(0, top),
                max(1, box_width),
                max(1, box_height),
            )
        )
    return OcrTextResult(tuple(restored), result.text, result.average_confidence)
