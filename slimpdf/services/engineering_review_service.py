"""Fuse OCR, correction, geometry, and external evidence for human review."""

from __future__ import annotations

import re

from slimpdf.engineering_ocr_models import (
    CrossSheetValidationReport,
    EngineeringFusionConfig,
    EngineeringNormalizationReport,
    EngineeringReviewBundle,
    EngineeringReviewItem,
    EngineeringReviewStatus,
    TextEvidence,
    TextSymbolRelationship,
)


DEFAULT_ENGINEERING_FUSION_CONFIG = EngineeringFusionConfig()
TAG_TOKEN_PATTERN = re.compile(r"\b[A-Z]{1,6}[-_][A-Z0-9]{1,12}\b", re.IGNORECASE)


def build_engineering_review_bundle(
    texts: tuple[TextEvidence, ...],
    normalization_reports: tuple[EngineeringNormalizationReport, ...],
    relationships: tuple[TextSymbolRelationship, ...] = (),
    external_report: CrossSheetValidationReport | None = None,
    *,
    config: EngineeringFusionConfig = DEFAULT_ENGINEERING_FUSION_CONFIG,
) -> EngineeringReviewBundle:
    """Build auditable decisions without changing text or approving candidates."""

    config.validate()
    if len(texts) != len(normalization_reports):
        raise ValueError("Text evidence와 normalization report 개수가 일치해야 합니다.")
    items = tuple(
        _build_item(
            index,
            text,
            normalization_reports[index],
            relationships,
            external_report,
            config,
        )
        for index, text in enumerate(texts)
    )
    return EngineeringReviewBundle(items)


def _build_item(
    index: int,
    text: TextEvidence,
    normalization: EngineeringNormalizationReport,
    relationships: tuple[TextSymbolRelationship, ...],
    external_report: CrossSheetValidationReport | None,
    config: EngineeringFusionConfig,
) -> EngineeringReviewItem:
    if normalization.raw_text != text.text:
        raise ValueError("Normalization 원문이 TextEvidence 원문과 일치하지 않습니다.")
    related = tuple(item for item in relationships if item.text_index == index)
    tags = {_tag_key(value) for value in TAG_TOKEN_PATTERN.findall(text.text)}
    external_issues = (
        tuple(
            issue
            for issue in external_report.issues
            if _tag_key(issue.tag) in tags
        )
        if external_report is not None
        else ()
    )

    weighted_scores = [(_unit_confidence(text.confidence), 0.70)]
    if related:
        weighted_scores.append((max(item.score for item in related), 0.15))
    if external_report is not None and tags:
        observed = set(external_report.observed_tags)
        validated = all(tag in observed for tag in tags) and not external_issues
        weighted_scores.append((1.0 if validated else 0.0, 0.15))
    total_weight = sum(weight for _score, weight in weighted_scores)
    fused = sum(score * weight for score, weight in weighted_scores) / total_weight

    reasons: list[str] = []
    ocr_confidence = _unit_confidence(text.confidence)
    if ocr_confidence < config.low_ocr_threshold:
        reasons.append("low_ocr_confidence")
    if normalization.requires_review:
        reasons.append("normalization_candidate")
    if external_issues:
        reasons.append("external_validation_issue")
    if related:
        reasons.append("geometry_relationship")
    if fused < config.pass_threshold:
        reasons.append("fused_confidence_below_threshold")

    blocking_review = (
        ocr_confidence < config.low_ocr_threshold
        or normalization.requires_review
        or bool(external_issues)
        or fused < config.pass_threshold
    )
    status = EngineeringReviewStatus.REVIEW if blocking_review else EngineeringReviewStatus.PASS
    if (
        config.allow_auto_correct
        and normalization.candidates
        and not external_issues
        and ocr_confidence >= config.pass_threshold
        and all(
            candidate.confidence >= config.auto_correct_threshold
            for candidate in normalization.candidates
        )
    ):
        status = EngineeringReviewStatus.AUTO_CORRECT
        reasons.append("auto_correct_policy_candidate")

    return EngineeringReviewItem(
        index,
        text.text,
        text.bounding_box,
        status,
        round(fused, 6),
        tuple(dict.fromkeys(reasons)),
        normalization.candidates,
        tuple(dict.fromkeys(item.symbol_id for item in related)),
        external_issues,
    )


def _unit_confidence(value: float) -> float:
    numeric = float(value)
    if numeric > 1.0:
        numeric /= 100.0
    return max(0.0, min(1.0, numeric))


def _tag_key(value: str) -> str:
    return "".join(value.upper().split())
