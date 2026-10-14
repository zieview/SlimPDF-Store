"""Fail-closed comparison of two runs on the same reviewed OCR dataset."""

from __future__ import annotations

from dataclasses import dataclass

from slimpdf.ocr_benchmark import BenchmarkReport

REQUIRED_CATEGORIES = frozenset({
    "horizontal_text", "vertical_text", "small_text", "equipment_tag",
    "pipe_size", "dimension", "temperature", "pressure", "refrigerant",
    "title_block", "leader_label", "dense_area", "low_quality_scan",
    "blurred_scan", "rotated_text",
})


@dataclass(frozen=True, slots=True)
class BenchmarkGateResult:
    passed: bool
    reasons: tuple[str, ...]


def compare_benchmark_reports(
    baseline: BenchmarkReport,
    candidate: BenchmarkReport,
) -> BenchmarkGateResult:
    """Reject incomparable datasets or any engineering-critical regression."""

    if baseline.schema != candidate.schema or baseline.sample_count != candidate.sample_count:
        return BenchmarkGateResult(False, ("benchmark schema 또는 샘플 수가 다릅니다.",))
    before = {sample.sample_id: sample for sample in baseline.samples}
    after = {sample.sample_id: sample for sample in candidate.samples}
    if len(before) != baseline.sample_count or len(after) != candidate.sample_count:
        return BenchmarkGateResult(False, ("benchmark sample ID가 중복됐습니다.",))
    if before.keys() != after.keys() or any(
        (before[key].category, before[key].expected)
        != (after[key].category, after[key].expected)
        for key in before
    ):
        return BenchmarkGateResult(False, ("동일한 정답 dataset의 결과가 아닙니다.",))

    reasons: list[str] = []
    for key in sorted(before):
        old, new = before[key], after[key]
        if old.tag_exact_match and not new.tag_exact_match:
            reasons.append(f"{key}: TAG exact match 퇴행")
        if old.numeric_exact_match and not new.numeric_exact_match:
            reasons.append(f"{key}: 수치 exact match 퇴행")
        if old.critical_token_exact_match and not new.critical_token_exact_match:
            reasons.append(f"{key}: Critical Engineering Error 증가")
    if candidate.tag_exact_match_rate < baseline.tag_exact_match_rate:
        reasons.append("전체 TAG exact match 퇴행")
    if candidate.numeric_exact_match_rate < baseline.numeric_exact_match_rate:
        reasons.append("전체 수치 exact match 퇴행")
    if candidate.critical_engineering_error_rate > baseline.critical_engineering_error_rate:
        reasons.append("전체 Critical Engineering Error Rate 증가")
    return BenchmarkGateResult(not reasons, tuple(reasons))


def check_benchmark_coverage(report: BenchmarkReport) -> BenchmarkGateResult:
    """Require every agreed engineering category before claiming full validation."""

    present = {sample.category for sample in report.samples}
    missing = sorted(REQUIRED_CATEGORIES - present)
    if missing:
        return BenchmarkGateResult(
            False, ("승인 dataset의 필수 범주 누락: " + ", ".join(missing),)
        )
    return BenchmarkGateResult(True, ())
