"""Deterministic JSON/CSV export for engineering drawing evidence."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import unicodedata
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from slimpdf.engineering_ocr_models import (
    EngineeringPageBundle,
    EngineeringValueObservation,
    NumericalConsistencyIssue,
)
from slimpdf.services.engineering_numerical_service import (
    validate_numerical_consistency,
)


SCHEMA_VERSION = 2
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024


def export_engineering_analysis(
    source_path: Path,
    pages: tuple[EngineeringPageBundle, ...],
    output_directory: Path,
    *,
    numerical_observations: tuple[EngineeringValueObservation, ...] = (),
    numerical_issues: tuple[NumericalConsistencyIssue, ...] = (),
) -> Path:
    """Write a self-contained export directory without replacing existing data."""

    source = source_path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"구조화 내보내기 원본을 찾을 수 없습니다: {source}")
    if source.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("구조화 내보내기 원본은 2GB를 초과할 수 없습니다.")
    _validate_pages(pages)
    if validate_numerical_consistency(numerical_observations) != numerical_issues:
        raise ValueError("수치 관측값과 충돌 보고서가 일치하지 않습니다.")
    target = output_directory.expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"기존 구조화 결과를 덮어쓸 수 없습니다: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    temporary.mkdir()
    try:
        source_sha256 = _sha256(source)
        document_id = f"doc-{source_sha256[:24]}"
        manifest, text_rows, relationship_rows, issue_rows, value_rows = _build_rows(
            source,
            source_sha256,
            document_id,
            pages,
            numerical_observations,
            numerical_issues,
        )
        _write_json(temporary / "engineering_analysis.json", manifest)
        _write_csv(
            temporary / "texts.csv",
            (
                "document_id", "page_id", "text_id", "page_number", "raw_text",
                "x0", "y0", "x1", "y1", "status", "fused_confidence", "reasons",
            ),
            text_rows,
        )
        _write_csv(
            temporary / "relationships.csv",
            (
                "document_id", "page_id", "text_id", "symbol_id", "score",
                "relationship_id",
                "distance_score", "connectivity_score", "alignment_score",
                "semantic_score", "reasons",
            ),
            relationship_rows,
        )
        _write_csv(
            temporary / "issues.csv",
            (
                "document_id", "page_id", "text_id", "issue_kind", "tag",
                "message", "provenance",
            ),
            issue_rows,
        )
        _write_csv(
            temporary / "values.csv",
            (
                "document_id", "page_id", "text_id", "kind", "raw_text",
                "normalized_text", "value_text", "unit", "start", "end",
            ),
            value_rows,
        )
        _write_csv(
            temporary / "numerical_issues.csv",
            (
                "issue_kind", "entity_key", "value_kind", "unit",
                "observed_values", "provenance",
            ),
            [
                (
                    issue.kind.value, issue.entity_key, issue.value_kind.value,
                    issue.unit or "", "|".join(issue.observed_values),
                    "|".join(issue.provenance),
                )
                for issue in numerical_issues
            ],
        )
        _write_csv(
            temporary / "value_observations.csv",
            (
                "entity_key", "value_kind", "value_text", "unit",
                "provenance", "confidence",
            ),
            [
                (
                    item.entity_key, item.kind.value, item.value_text,
                    item.unit or "", item.provenance, item.confidence,
                )
                for item in numerical_observations
            ],
        )
        _validate_export(temporary)
        os.rename(temporary, target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return target


def _validate_pages(pages: tuple[EngineeringPageBundle, ...]) -> None:
    numbers = [page.source.page_number for page in pages]
    if any(number <= 0 for number in numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("페이지 번호는 양수이며 중복될 수 없습니다.")
    for page in pages:
        if len(page.review.items) != len(page.texts):
            raise ValueError("페이지 text evidence와 review item 개수가 일치하지 않습니다.")
        for index, item in enumerate(page.review.items):
            if item.text_index != index:
                raise ValueError("review text_index가 source evidence 순서와 일치하지 않습니다.")
            if item.raw_text != page.texts[index].text:
                raise ValueError("review 원문과 text evidence 원문이 일치하지 않습니다.")
        for value in page.values:
            if value.text_index < 0 or value.text_index >= len(page.texts):
                raise ValueError("Engineering value의 text_index가 범위를 벗어났습니다.")
            evidence = value.evidence
            source_text = page.texts[value.text_index].text
            if source_text[evidence.start:evidence.end] != evidence.raw_text:
                raise ValueError("Engineering value 원문 범위가 text evidence와 일치하지 않습니다.")


def _build_rows(
    source, source_sha256, document_id, pages,
    numerical_observations, numerical_issues,
):
    page_objects = []
    text_rows = []
    relationship_rows = []
    issue_rows = []
    value_rows = []
    for page in sorted(pages, key=lambda item: item.source.page_number):
        page_id = f"{document_id}:page:{page.source.page_number}"
        text_ids = {
            index: f"{page_id}:text:{index + 1}"
            for index in range(len(page.review.items))
        }
        for item in page.review.items:
            text_id = text_ids[item.text_index]
            x0, y0, x1, y1 = item.bounding_box
            text_rows.append(
                (
                    document_id, page_id, text_id, page.source.page_number,
                    item.raw_text, x0, y0, x1, y1, item.status.value,
                    item.fused_confidence, "|".join(item.reasons),
                )
            )
            for issue in item.external_issues:
                issue_rows.append(
                    (
                        document_id, page_id, text_id, issue.kind.value, issue.tag,
                        issue.message, "|".join(issue.provenance),
                    )
                )
        values_by_text: dict[int, list[object]] = {}
        for value in page.values:
            values_by_text.setdefault(value.text_index, []).append(value.evidence)
            evidence = value.evidence
            value_rows.append(
                (
                    document_id, page_id, text_ids[value.text_index],
                    evidence.kind.value, evidence.raw_text,
                    evidence.normalized_text, evidence.value_text,
                    evidence.unit or "", evidence.start, evidence.end,
                )
            )
        for relationship in page.relationships:
            if relationship.text_index not in text_ids:
                raise ValueError("관계의 text_index가 페이지 text evidence 범위를 벗어났습니다.")
            relationship_id = (
                f"{page_id}:relationship:{relationship.text_index + 1}:"
                f"{relationship.symbol_id}"
            )
            relationship_rows.append(
                (
                    document_id, page_id, text_ids[relationship.text_index],
                    relationship.symbol_id, relationship.score, relationship_id,
                    relationship.distance_score, relationship.connectivity_score,
                    relationship.alignment_score, relationship.semantic_score,
                    "|".join(relationship.reasons),
                )
            )
        page_objects.append(
            {
                "page_id": page_id,
                "page_number": page.source.page_number,
                "source_kind": page.source.source_kind.value,
                "raster_coverage": page.source.raster_coverage,
                "vector_path_count": page.source.vector_path_count,
                "texts": [
                    {
                        "text_id": text_ids[item.text_index],
                        "source": page.texts[item.text_index].source.value,
                        "source_confidence": page.texts[item.text_index].confidence,
                        "engineering_values": [
                            asdict(value)
                            for value in values_by_text.get(item.text_index, ())
                        ],
                        **asdict(item),
                    }
                    for item in page.review.items
                ],
                "relationships": [
                    {
                        "relationship_id": (
                            f"{page_id}:relationship:{item.text_index + 1}:"
                            f"{item.symbol_id}"
                        ),
                        "text_id": text_ids[item.text_index],
                        **asdict(item),
                    }
                    for item in page.relationships
                ],
            }
        )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "document": {
            "document_id": document_id,
            "source_name": source.name,
            "source_sha256": source_sha256,
            "page_count": len(pages),
        },
        "pages": page_objects,
        "value_observations": [asdict(item) for item in numerical_observations],
        "numerical_issues": [asdict(issue) for issue in numerical_issues],
    }
    return manifest, text_rows, relationship_rows, issue_rows, value_rows


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )


def _json_default(value: object) -> object:
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"JSON으로 변환할 수 없는 값입니다: {type(value).__name__}")


def _write_csv(path: Path, headers: tuple[str, ...], rows: list[tuple[object, ...]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(tuple(_spreadsheet_safe_cell(cell) for cell in row) for row in rows)


def _spreadsheet_safe_cell(value: object) -> object:
    """Keep untrusted CSV text literal when opened by spreadsheet programs.

    JSON remains the lossless evidence record; the leading apostrophe is only
    applied to a CSV display cell with a formula prefix (including whitespace
    or control-character prefixes that some importers ignore).
    """

    if not isinstance(value, str):
        return value
    index = 0
    while index < len(value) and (
        value[index].isspace() or unicodedata.category(value[index]) in {"Cc", "Cf"}
    ):
        index += 1
    return "'" + value if value[index:index + 1] in {"=", "+", "-", "@"} else value


def _validate_export(directory: Path) -> None:
    expected = {
        "engineering_analysis.json", "texts.csv", "relationships.csv", "issues.csv",
        "values.csv",
        "numerical_issues.csv",
        "value_observations.csv",
    }
    actual = {path.name for path in directory.iterdir() if path.is_file()}
    if actual != expected:
        raise RuntimeError("구조화 내보내기 파일 구성이 올바르지 않습니다.")
    value = json.loads((directory / "engineering_analysis.json").read_text(encoding="utf-8"))
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("구조화 내보내기 schema version이 올바르지 않습니다.")
    for name in expected:
        if (directory / name).stat().st_size <= 0:
            raise RuntimeError(f"구조화 내보내기 파일이 비어 있습니다: {name}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
