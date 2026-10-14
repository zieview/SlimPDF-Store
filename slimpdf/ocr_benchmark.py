"""Deterministic metrics for engineering-drawing OCR benchmark datasets."""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from slimpdf.ocr_models import OcrTextResult


TAG_PATTERN = re.compile(r"\b[A-Z]{1,6}[-_]?[A-Z0-9]{1,8}\b", re.IGNORECASE)
NUMBER_PATTERN = re.compile(
    r"(?:DN\s*\d+|[Ø⌀]\s*\d+(?:\.\d+)?|[-+]?\d+(?:\.\d+)?\s*(?:mm|bar|kPa|MPa|°?C|V|Hz)?)",
    re.IGNORECASE,
)
Recognizer = Callable[[Path, tuple[str, ...]], OcrTextResult]


@dataclass(frozen=True, slots=True)
class BenchmarkSample:
    """One reviewed image crop and its exact engineering ground truth."""

    sample_id: str
    image_path: Path
    category: str
    ground_truth: str
    critical_tokens: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BenchmarkSampleResult:
    sample_id: str
    category: str
    expected: str
    recognized: str
    character_error_rate: float
    word_error_rate: float
    exact_match: bool
    tag_exact_match: bool
    numeric_exact_match: bool
    critical_token_exact_match: bool
    average_confidence: float
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    schema: int
    sample_count: int
    character_error_rate: float
    word_error_rate: float
    exact_match_rate: float
    tag_exact_match_rate: float
    numeric_exact_match_rate: float
    critical_engineering_error_rate: float
    average_seconds_per_sample: float
    categories: dict[str, dict[str, float | int]]
    samples: tuple[BenchmarkSampleResult, ...]

    def write_json(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(destination)


def load_benchmark_manifest(path: Path) -> tuple[BenchmarkSample, ...]:
    """Load reviewed samples while preventing paths outside the dataset root."""

    manifest = path.resolve()
    root = manifest.parent
    value = json.loads(manifest.read_text(encoding="utf-8"))
    rows = value.get("samples") if isinstance(value, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError("OCR benchmark manifest에는 samples가 1개 이상 필요합니다.")
    samples: list[BenchmarkSample] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("OCR benchmark sample 형식이 올바르지 않습니다.")
        sample_id = str(row.get("id", "")).strip()
        category = str(row.get("category", "")).strip()
        ground_truth = str(row.get("ground_truth", "")).strip()
        candidate = (root / str(row.get("image", ""))).resolve()
        if not sample_id or sample_id in seen or not category or not ground_truth:
            raise ValueError("sample id/category/ground_truth가 누락되거나 중복됐습니다.")
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise ValueError("benchmark image 경로가 dataset 밖을 가리킵니다.") from error
        if not candidate.is_file():
            raise FileNotFoundError(f"benchmark image를 찾을 수 없습니다: {candidate}")
        tokens = tuple(str(item).strip() for item in row.get("critical_tokens", ()) if str(item).strip())
        samples.append(BenchmarkSample(sample_id, candidate, category, ground_truth, tokens))
        seen.add(sample_id)
    return tuple(samples)


def run_benchmark(
    samples: tuple[BenchmarkSample, ...],
    recognizer: Recognizer,
    languages: tuple[str, ...] = ("kor", "eng"),
) -> BenchmarkReport:
    """Run one reproducible baseline and aggregate engineering-critical metrics."""

    if not samples:
        raise ValueError("OCR benchmark sample이 없습니다.")
    results: list[BenchmarkSampleResult] = []
    for sample in samples:
        started = time.perf_counter()
        recognized = recognizer(sample.image_path, languages)
        elapsed = time.perf_counter() - started
        expected_text = _normalize(sample.ground_truth)
        actual_text = _normalize(recognized.text)
        expected_tags = _tokens(TAG_PATTERN, expected_text)
        actual_tags = _tokens(TAG_PATTERN, actual_text)
        expected_numbers = _tokens(NUMBER_PATTERN, expected_text)
        actual_numbers = _tokens(NUMBER_PATTERN, actual_text)
        critical_ok = all(
            _contains_exact_token(actual_text, _normalize(token))
            for token in sample.critical_tokens
        )
        results.append(
            BenchmarkSampleResult(
                sample.sample_id,
                sample.category,
                expected_text,
                actual_text,
                _error_rate(expected_text, actual_text),
                _error_rate(expected_text.split(), actual_text.split()),
                expected_text == actual_text,
                expected_tags == actual_tags,
                expected_numbers == actual_numbers,
                critical_ok,
                recognized.average_confidence,
                elapsed,
            )
        )
    categories: dict[str, dict[str, float | int]] = {}
    grouped: dict[str, list[BenchmarkSampleResult]] = defaultdict(list)
    for result in results:
        grouped[result.category].append(result)
    for category, items in sorted(grouped.items()):
        categories[category] = _summary(items)
    summary = _summary(results)
    return BenchmarkReport(
        1,
        len(results),
        float(summary["character_error_rate"]),
        float(summary["word_error_rate"]),
        float(summary["exact_match_rate"]),
        float(summary["tag_exact_match_rate"]),
        float(summary["numeric_exact_match_rate"]),
        float(summary["critical_engineering_error_rate"]),
        float(summary["average_seconds_per_sample"]),
        categories,
        tuple(results),
    )


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split()).casefold()


def _contains_exact_token(text: str, token: str) -> bool:
    """Match a critical value without accepting a longer adjacent identifier."""

    return bool(token) and re.search(
        rf"(?<!\w){re.escape(token)}(?!\w)", text
    ) is not None


def _tokens(pattern: re.Pattern[str], value: str) -> tuple[str, ...]:
    return tuple(sorted(match.group(0).replace(" ", "").casefold() for match in pattern.finditer(value)))


def _error_rate(expected, actual) -> float:
    left = list(expected)
    right = list(actual)
    if not left:
        return 0.0 if not right else 1.0
    previous = list(range(len(right) + 1))
    for row, source in enumerate(left, start=1):
        current = [row]
        for column, target in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column] + 1,
                    previous[column - 1] + (source != target),
                )
            )
        previous = current
    return previous[-1] / len(left)


def _summary(items: list[BenchmarkSampleResult]) -> dict[str, float | int]:
    count = len(items)
    return {
        "sample_count": count,
        "character_error_rate": sum(item.character_error_rate for item in items) / count,
        "word_error_rate": sum(item.word_error_rate for item in items) / count,
        "exact_match_rate": sum(item.exact_match for item in items) / count,
        "tag_exact_match_rate": sum(item.tag_exact_match for item in items) / count,
        "numeric_exact_match_rate": sum(item.numeric_exact_match for item in items) / count,
        "critical_engineering_error_rate": 1.0 - sum(item.critical_token_exact_match for item in items) / count,
        "average_seconds_per_sample": sum(item.elapsed_seconds for item in items) / count,
    }
