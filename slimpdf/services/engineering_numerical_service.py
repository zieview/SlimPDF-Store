"""Conservative consistency checks for explicitly associated engineering values."""

from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from slimpdf.engineering_ocr_models import (
    EngineeringValueKind,
    EngineeringValueObservation,
    EngineeringPageBundle,
    NumericalConsistencyIssue,
    NumericalConsistencyIssueKind,
)


MAX_OBSERVATIONS = 1_000_000
INLINE_TAG_PATTERN = re.compile(
    r"\b[A-Z]{1,6}[-_][A-Z0-9]{1,12}\b", re.IGNORECASE
)
_NUMERIC_KINDS = {
    EngineeringValueKind.PIPE_SIZE,
    EngineeringValueKind.DIAMETER,
    EngineeringValueKind.PRESSURE,
    EngineeringValueKind.TEMPERATURE,
    EngineeringValueKind.DIMENSION,
    EngineeringValueKind.VOLTAGE,
}


def validate_numerical_consistency(
    observations: tuple[EngineeringValueObservation, ...],
) -> tuple[NumericalConsistencyIssue, ...]:
    """Report conflicts only within an explicit entity/kind/unit group."""

    if len(observations) > MAX_OBSERVATIONS:
        raise ValueError("Engineering 수치 관측값은 1,000,000개를 초과할 수 없습니다.")
    grouped: dict[
        tuple[str, EngineeringValueKind, str | None],
        list[tuple[str, str, str]],
    ] = defaultdict(list)
    for observation in observations:
        entity = _entity_key(observation.entity_key)
        provenance = observation.provenance.strip()
        value = observation.value_text.strip()
        if not entity or not provenance or not value:
            raise ValueError("수치 관측값의 entity, value, provenance는 비어 있을 수 없습니다.")
        if not isinstance(observation.kind, EngineeringValueKind):
            raise ValueError("지원하지 않는 Engineering value 종류입니다.")
        if not 0.0 <= observation.confidence <= 1.0:
            raise ValueError("수치 관측 confidence는 0~1 범위여야 합니다.")
        unit = _unit_key(observation.unit)
        canonical = _canonical_value(observation.kind, value)
        grouped[(entity, observation.kind, unit)].append(
            (canonical, value, provenance)
        )

    issues = []
    for (entity, kind, unit), rows in sorted(
        grouped.items(), key=lambda item: (item[0][0], item[0][1].value, item[0][2] or "")
    ):
        distinct = {canonical for canonical, _raw, _source in rows}
        if len(distinct) <= 1:
            continue
        issues.append(
            NumericalConsistencyIssue(
                NumericalConsistencyIssueKind.CONFLICTING_VALUE,
                entity,
                kind,
                unit,
                tuple(dict.fromkeys(raw for _canonical, raw, _source in rows)),
                tuple(source for _canonical, _raw, source in rows),
            )
        )
    return tuple(issues)


def build_inline_value_observations(
    pages: tuple[EngineeringPageBundle, ...],
) -> tuple[EngineeringValueObservation, ...]:
    """Associate values only when exactly one TAG exists in the same text evidence."""

    observations = []
    for page in pages:
        values_by_text: dict[int, list[object]] = defaultdict(list)
        for record in page.values:
            values_by_text[record.text_index].append(record.evidence)
        for text_index, values in values_by_text.items():
            if text_index < 0 or text_index >= len(page.texts):
                raise ValueError("Engineering value의 text_index가 범위를 벗어났습니다.")
            text = page.texts[text_index]
            tags = tuple(
                dict.fromkeys(match.group(0).upper() for match in INLINE_TAG_PATTERN.finditer(text.text))
            )
            if len(tags) != 1:
                continue
            provenance = f"page={page.source.page_number};text={text_index + 1}"
            observations.extend(
                EngineeringValueObservation(
                    tags[0], value.kind, value.value_text, value.unit,
                    provenance, text.confidence,
                )
                for value in values
            )
    return tuple(observations)


def _entity_key(value: str) -> str:
    return "".join(value.split()).upper()


def _unit_key(value: str | None) -> str | None:
    return value.strip().casefold() if value and value.strip() else None


def _canonical_value(kind: EngineeringValueKind, value: str) -> str:
    if kind not in _NUMERIC_KINDS:
        return value.upper()
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"숫자 Engineering value 형식이 올바르지 않습니다: {value}") from error
    if not number.is_finite():
        raise ValueError("Engineering value는 유한한 숫자여야 합니다.")
    return format(number.normalize(), "f")
