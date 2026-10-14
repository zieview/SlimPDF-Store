"""Conservative typed-value extraction for engineering text evidence."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from slimpdf.engineering_ocr_models import (
    EngineeringValueEvidence,
    EngineeringValueKind,
)


@dataclass(frozen=True, slots=True)
class _ValuePattern:
    kind: EngineeringValueKind
    expression: re.Pattern[str]
    unit: str | None


_UNSIGNED_NUMBER = r"(?:\d+(?:\.\d+)?|\.\d+)"
_SIGNED_NUMBER = rf"[+-]?{_UNSIGNED_NUMBER}"
_PATTERNS = (
    _ValuePattern(
        EngineeringValueKind.PIPE_SIZE,
        re.compile(rf"\bDN\s*(?P<value>{_UNSIGNED_NUMBER})\b", re.IGNORECASE),
        "mm",
    ),
    _ValuePattern(
        EngineeringValueKind.DIAMETER,
        re.compile(rf"[Ø⌀]\s*(?P<value>{_UNSIGNED_NUMBER})(?:\s*mm)?\b", re.IGNORECASE),
        "mm",
    ),
    _ValuePattern(
        EngineeringValueKind.TEMPERATURE,
        re.compile(rf"(?<![\w.])(?P<value>{_SIGNED_NUMBER})\s*°?\s*C\b", re.IGNORECASE),
        "°C",
    ),
    _ValuePattern(
        EngineeringValueKind.PRESSURE,
        re.compile(
            rf"(?<![\w.])(?P<value>{_SIGNED_NUMBER})\s*(?P<unit>bar|kPa|MPa)\b",
            re.IGNORECASE,
        ),
        None,
    ),
    _ValuePattern(
        EngineeringValueKind.VOLTAGE,
        re.compile(
            rf"(?<![\w.+-])(?P<value>{_UNSIGNED_NUMBER})\s*(?P<unit>VAC|VDC|V)\b",
            re.IGNORECASE,
        ),
        None,
    ),
    _ValuePattern(
        EngineeringValueKind.REFRIGERANT,
        re.compile(r"\bR[- ]?(?P<value>\d{3,4}[A-Z]?)\b", re.IGNORECASE),
        None,
    ),
    _ValuePattern(
        EngineeringValueKind.DIMENSION,
        re.compile(rf"(?<![\w.+-])(?P<value>{_UNSIGNED_NUMBER})\s*mm\b", re.IGNORECASE),
        "mm",
    ),
)


def extract_engineering_values(text: str) -> tuple[EngineeringValueEvidence, ...]:
    """Extract only explicit values; never infer or correct uncertain characters."""

    normalized_source = unicodedata.normalize("NFC", text)
    if normalized_source != text:
        # NFC can alter string indices, so preserve exact source offsets by refusing it.
        normalized_source = text
    matches: list[EngineeringValueEvidence] = []
    occupied: list[tuple[int, int]] = []
    for spec in _PATTERNS:
        for match in spec.expression.finditer(normalized_source):
            start, end = match.span()
            if any(start < used_end and end > used_start for used_start, used_end in occupied):
                continue
            raw = match.group(0)
            value = match.group("value")
            unit_group = match.groupdict().get("unit")
            unit = _canonical_unit(unit_group) if unit_group else spec.unit
            matches.append(
                EngineeringValueEvidence(
                    raw,
                    _normalized_value(spec.kind, value, unit),
                    spec.kind,
                    value,
                    unit,
                    start,
                    end,
                )
            )
            occupied.append((start, end))
    return tuple(sorted(matches, key=lambda item: (item.start, item.end)))


def _canonical_unit(unit: str) -> str:
    key = unit.casefold()
    return {
        "bar": "bar",
        "kpa": "kPa",
        "mpa": "MPa",
        "v": "V",
        "vac": "VAC",
        "vdc": "VDC",
    }[key]


def _normalized_value(
    kind: EngineeringValueKind, value: str, unit: str | None
) -> str:
    if kind is EngineeringValueKind.PIPE_SIZE:
        return f"DN{value}"
    if kind is EngineeringValueKind.DIAMETER:
        return f"Ø{value} mm"
    if kind is EngineeringValueKind.REFRIGERANT:
        return f"R{value.upper()}"
    if unit == "°C":
        return f"{value} °C"
    return f"{value} {unit}" if unit else value
