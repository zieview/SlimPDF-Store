"""Non-destructive normalization candidates for engineering OCR text."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from slimpdf.engineering_ocr_models import (
    EngineeringCorrectionCandidate,
    EngineeringNormalizationReport,
    EngineeringReviewStatus,
    EngineeringTokenKind,
)


MAX_DICTIONARY_BYTES = 5 * 1024 * 1024
TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"[A-Za-z0-9Ø⌀][A-Za-z0-9_./+\-Ø⌀°]{2,}"
    r"(?:\s*(?:mm|bar|kPa|MPa|°?C|V|Hz))?"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
UNIT_PATTERN = re.compile(r"(?:mm|bar|kPa|MPa|°?C|V|Hz)$", re.IGNORECASE)
AMBIGUOUS_REPLACEMENTS = {"O": "0", "o": "0", "I": "1", "l": "1"}


@dataclass(frozen=True, slots=True)
class EngineeringDictionary:
    """Validated canonical terms indexed by their explicitly approved aliases."""

    canonical_by_alias: dict[str, str]


def load_engineering_dictionary(path: Path) -> EngineeringDictionary:
    """Load a small JSON dictionary without accepting ambiguous duplicate aliases."""

    if not path.is_file():
        raise FileNotFoundError(f"Engineering OCR 사전을 찾을 수 없습니다: {path}")
    if path.stat().st_size > MAX_DICTIONARY_BYTES:
        raise ValueError("Engineering OCR 사전은 5MB를 초과할 수 없습니다.")
    value = json.loads(path.read_text(encoding="utf-8"))
    rows = value.get("terms") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Engineering OCR 사전에는 terms 배열이 필요합니다.")
    aliases: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Engineering OCR 사전 term 형식이 올바르지 않습니다.")
        canonical = str(row.get("canonical", "")).strip()
        raw_aliases = row.get("aliases", [])
        if not canonical or not isinstance(raw_aliases, list):
            raise ValueError("canonical 또는 aliases가 올바르지 않습니다.")
        for alias_value in raw_aliases:
            alias = str(alias_value).strip()
            if not alias or alias == canonical:
                continue
            key = _dictionary_key(alias)
            previous = aliases.get(key)
            if previous is not None and previous != canonical:
                raise ValueError(f"중복된 사전 alias입니다: {alias}")
            aliases[key] = canonical
    return EngineeringDictionary(aliases)


def analyze_engineering_text(
    text: str,
    dictionary: EngineeringDictionary | None = None,
) -> EngineeringNormalizationReport:
    """Return review candidates while preserving the exact OCR source text."""

    candidates: list[EngineeringCorrectionCandidate] = []
    seen_spans: set[tuple[int, int, str]] = set()
    for match in TOKEN_PATTERN.finditer(text):
        raw = match.group(0)
        suggestion: str | None = None
        reason = ""
        kind = EngineeringTokenKind.TAG
        confidence = 0.55

        if dictionary is not None:
            suggestion = dictionary.canonical_by_alias.get(_dictionary_key(raw))
            if suggestion is not None:
                reason = "승인된 Engineering OCR 사전 alias와 일치"
                kind = EngineeringTokenKind.DICTIONARY
                confidence = 0.95

        if suggestion is None:
            suggestion = _ambiguous_character_candidate(raw)
            if suggestion is not None:
                reason = "숫자 인접 O/I/l 문자의 OCR 혼동 가능성"
                kind = (
                    EngineeringTokenKind.VALUE
                    if UNIT_PATTERN.search(raw)
                    else EngineeringTokenKind.TAG
                )

        if suggestion is None or suggestion == raw:
            continue
        key = (match.start(), match.end(), suggestion)
        if key in seen_spans:
            continue
        seen_spans.add(key)
        candidates.append(
            EngineeringCorrectionCandidate(
                raw,
                suggestion,
                match.start(),
                match.end(),
                kind,
                reason,
                confidence,
            )
        )
    return EngineeringNormalizationReport(text, tuple(candidates))


def apply_approved_candidates(
    report: EngineeringNormalizationReport,
    approved_indexes: set[int],
) -> str:
    """Apply only explicitly approved, non-overlapping candidates."""

    selected: list[EngineeringCorrectionCandidate] = []
    for index in sorted(approved_indexes):
        if index < 0 or index >= len(report.candidates):
            raise IndexError(f"교정 후보 번호가 범위를 벗어났습니다: {index}")
        selected.append(report.candidates[index])
    ordered = sorted(selected, key=lambda item: item.start)
    for previous, current in zip(ordered, ordered[1:]):
        if previous.end > current.start:
            raise ValueError("서로 겹치는 교정 후보는 동시에 적용할 수 없습니다.")
    corrected = report.raw_text
    for candidate in reversed(ordered):
        corrected = (
            corrected[: candidate.start]
            + candidate.suggested_text
            + corrected[candidate.end :]
        )
    return corrected


def approve_candidate(
    candidate: EngineeringCorrectionCandidate,
) -> EngineeringCorrectionCandidate:
    """Return an auditable approved copy without mutating the source candidate."""

    return EngineeringCorrectionCandidate(
        candidate.raw_text,
        candidate.suggested_text,
        candidate.start,
        candidate.end,
        candidate.kind,
        candidate.reason,
        candidate.confidence,
        EngineeringReviewStatus.APPROVED,
    )


def _ambiguous_character_candidate(raw: str) -> str | None:
    characters = list(raw)
    changed = False
    for index, value in enumerate(characters):
        replacement = AMBIGUOUS_REPLACEMENTS.get(value)
        if replacement is None:
            continue
        before_is_digit = index > 0 and characters[index - 1].isdigit()
        after_is_digit = index + 1 < len(characters) and characters[index + 1].isdigit()
        if before_is_digit or after_is_digit:
            characters[index] = replacement
            changed = True
    return "".join(characters) if changed else None


def _dictionary_key(value: str) -> str:
    return "".join(value.upper().split())
