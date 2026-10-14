"""Conservative geometry-based relationships for engineering drawings."""

from __future__ import annotations

import math
from collections.abc import Callable

from slimpdf.engineering_ocr_models import (
    LineSegment,
    SymbolEvidence,
    TextEvidence,
    TextSymbolRelationship,
)


SemanticScorer = Callable[[str, SymbolEvidence], float]


def infer_text_symbol_relationships(
    texts: tuple[TextEvidence, ...],
    symbols: tuple[SymbolEvidence, ...],
    segments: tuple[LineSegment, ...],
    *,
    maximum_distance: float = 144.0,
    connection_tolerance: float = 8.0,
    minimum_score: float = 0.55,
    semantic_scorer: SemanticScorer | None = None,
) -> tuple[TextSymbolRelationship, ...]:
    """Score relationships without accepting proximity as sufficient evidence."""

    if maximum_distance <= 0 or connection_tolerance <= 0:
        raise ValueError("Geometry 거리와 연결 허용치는 0보다 커야 합니다.")
    relationships: list[TextSymbolRelationship] = []
    components = _connected_components(segments, connection_tolerance)
    for text_index, text in enumerate(texts):
        for symbol in symbols:
            distance = _box_distance(text.bounding_box, symbol.bounding_box)
            distance_score = max(0.0, 1.0 - distance / maximum_distance)
            alignment_score = _alignment_score(
                text.bounding_box, symbol.bounding_box, maximum_distance
            )
            connected = any(
                _component_touches_box(component, text.bounding_box, connection_tolerance)
                and _component_touches_box(
                    component, symbol.bounding_box, connection_tolerance
                )
                for component in components
            )
            connectivity_score = 1.0 if connected else 0.0
            semantic_score = _bounded_score(
                semantic_scorer(text.text, symbol) if semantic_scorer else 0.0
            )
            score = (
                0.30 * distance_score
                + 0.35 * connectivity_score
                + 0.20 * alignment_score
                + 0.15 * semantic_score
            )
            # Distance alone never creates a relationship. Require geometry
            # connectivity, or strong alignment plus independent semantics.
            supported = connected or (
                alignment_score >= 0.8 and semantic_score >= 0.7
            )
            if not supported or score < minimum_score:
                continue
            reasons = []
            if distance_score > 0:
                reasons.append("proximity")
            if connected:
                reasons.append("line_connectivity")
            if alignment_score >= 0.8:
                reasons.append("alignment")
            if semantic_score >= 0.7:
                reasons.append("semantic")
            relationships.append(
                TextSymbolRelationship(
                    text_index,
                    symbol.symbol_id,
                    round(score, 6),
                    round(distance_score, 6),
                    connectivity_score,
                    round(alignment_score, 6),
                    round(semantic_score, 6),
                    tuple(reasons),
                )
            )
    return tuple(sorted(relationships, key=lambda item: (-item.score, item.text_index, item.symbol_id)))


def _connected_components(
    segments: tuple[LineSegment, ...], tolerance: float
) -> tuple[tuple[LineSegment, ...], ...]:
    remaining = list(segments)
    components: list[tuple[LineSegment, ...]] = []
    while remaining:
        component = [remaining.pop()]
        changed = True
        while changed:
            changed = False
            pending: list[LineSegment] = []
            for candidate in remaining:
                if any(_segments_connect(candidate, current, tolerance) for current in component):
                    component.append(candidate)
                    changed = True
                else:
                    pending.append(candidate)
            remaining = pending
        components.append(tuple(component))
    return tuple(components)


def _segments_connect(first: LineSegment, second: LineSegment, tolerance: float) -> bool:
    return any(
        _point_distance(left, right) <= tolerance
        for left in (first.start, first.end)
        for right in (second.start, second.end)
    )


def _component_touches_box(
    component: tuple[LineSegment, ...],
    box: tuple[float, float, float, float],
    tolerance: float,
) -> bool:
    return any(
        _point_box_distance(point, box) <= tolerance
        for segment in component
        for point in (segment.start, segment.end)
    )


def _box_distance(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    dx = max(first[0] - second[2], second[0] - first[2], 0.0)
    dy = max(first[1] - second[3], second[1] - first[3], 0.0)
    return math.hypot(dx, dy)


def _alignment_score(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
    scale: float,
) -> float:
    first_center = ((first[0] + first[2]) / 2, (first[1] + first[3]) / 2)
    second_center = ((second[0] + second[2]) / 2, (second[1] + second[3]) / 2)
    offset = min(
        abs(first_center[0] - second_center[0]),
        abs(first_center[1] - second_center[1]),
    )
    return max(0.0, 1.0 - offset / scale)


def _point_box_distance(
    point: tuple[float, float], box: tuple[float, float, float, float]
) -> float:
    x = min(max(point[0], box[0]), box[2])
    y = min(max(point[1], box[1]), box[3])
    return math.hypot(point[0] - x, point[1] - y)


def _point_distance(first: tuple[float, float], second: tuple[float, float]) -> float:
    return math.hypot(first[0] - second[0], first[1] - second[1])


def _bounded_score(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
