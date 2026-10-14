"""Read-only BOM/list adapters and conservative cross-sheet TAG validation."""

from __future__ import annotations

import csv
import re
import zipfile
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree

from slimpdf.engineering_ocr_models import (
    CrossSheetIssueKind,
    CrossSheetValidationIssue,
    CrossSheetValidationReport,
    ExternalListKind,
    EngineeringReferenceSelection,
    ExternalTagRecord,
)


MAX_REFERENCE_BYTES = 50 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_REFERENCE_ROWS = 200_000
MAX_REFERENCE_FILES = 20
TAG_HEADERS = {
    "tag",
    "tagno",
    "tagnumber",
    "equipmentno",
    "equipmentnumber",
    "valveno",
    "valvenumber",
    "lineno",
    "linenumber",
    "태그",
    "태그번호",
    "기기번호",
    "밸브번호",
    "라인번호",
}
XML_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def load_external_tag_records(
    path: Path,
    list_kind: ExternalListKind,
    *,
    tag_column: str | None = None,
) -> tuple[ExternalTagRecord, ...]:
    """Load CSV or XLSX rows with provenance and strict TAG-column discovery."""

    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Engineering 참조 목록을 찾을 수 없습니다: {source}")
    if source.stat().st_size > MAX_REFERENCE_BYTES:
        raise ValueError("Engineering 참조 목록은 50MB를 초과할 수 없습니다.")
    suffix = source.suffix.casefold()
    if suffix == ".csv":
        sheets = (("CSV", _read_csv_rows(source)),)
    elif suffix == ".xlsx":
        sheets = _read_xlsx_sheets(source)
    else:
        raise ValueError("Engineering 참조 목록은 CSV 또는 XLSX만 지원합니다.")

    records: list[ExternalTagRecord] = []
    for sheet_name, rows in sheets:
        if not rows:
            continue
        headers = tuple(str(value).strip() for value in rows[0])
        tag_index = _find_tag_column(headers, tag_column)
        for row_number, row in enumerate(rows[1:], start=2):
            if len(records) >= MAX_REFERENCE_ROWS:
                raise ValueError("Engineering 참조 목록 행 수가 200,000개를 초과했습니다.")
            tag = str(row[tag_index]).strip() if tag_index < len(row) else ""
            if not tag:
                continue
            attributes = tuple(
                (header, str(row[index]).strip() if index < len(row) else "")
                for index, header in enumerate(headers)
                if index != tag_index and header
            )
            records.append(
                ExternalTagRecord(
                    tag,
                    list_kind,
                    str(source),
                    sheet_name,
                    row_number,
                    attributes,
                )
            )
    return tuple(records)


def load_reference_selections(
    selections: tuple[EngineeringReferenceSelection, ...],
) -> tuple[ExternalTagRecord, ...]:
    """Load a bounded set of distinct user-selected reference files."""

    if len(selections) > MAX_REFERENCE_FILES:
        raise ValueError("Engineering 참조 목록은 최대 20개까지 선택할 수 있습니다.")
    seen: set[tuple[Path, ExternalListKind, str]] = set()
    records: list[ExternalTagRecord] = []
    for selection in selections:
        column = (selection.tag_column or "").strip()
        key = (
            selection.path.expanduser().resolve(),
            selection.list_kind,
            column.casefold(),
        )
        if key in seen:
            raise ValueError(f"중복된 Engineering 참조 선택입니다: {selection.path}")
        seen.add(key)
        records.extend(
            load_external_tag_records(
                selection.path,
                selection.list_kind,
                tag_column=column or None,
            )
        )
    return tuple(records)


def validate_engineering_tags(
    observed_tags: tuple[str, ...],
    records: tuple[ExternalTagRecord, ...],
    *,
    comparison_fields: tuple[str, ...] = (),
    report_unobserved: bool = False,
) -> CrossSheetValidationReport:
    """Report external inconsistencies without modifying either source."""

    observed = tuple(dict.fromkeys(_tag_key(tag) for tag in observed_tags if tag.strip()))
    indexed: dict[str, list[ExternalTagRecord]] = defaultdict(list)
    for record in records:
        indexed[_tag_key(record.tag)].append(record)
    issues: list[CrossSheetValidationIssue] = []
    for tag in observed:
        matches = indexed.get(tag, [])
        if not matches:
            issues.append(
                CrossSheetValidationIssue(
                    CrossSheetIssueKind.MISSING_FROM_REFERENCE,
                    tag,
                    "도면 TAG가 외부 참조 목록에 없습니다.",
                    (),
                )
            )
            continue
        if len(matches) > 1:
            issues.append(
                CrossSheetValidationIssue(
                    CrossSheetIssueKind.DUPLICATE_REFERENCE,
                    tag,
                    "외부 참조 목록에 동일 TAG가 여러 번 존재합니다.",
                    tuple(_provenance(record) for record in matches),
                )
            )
        conflicts = _conflicting_fields(matches, comparison_fields)
        if conflicts:
            issues.append(
                CrossSheetValidationIssue(
                    CrossSheetIssueKind.CONFLICTING_REFERENCE,
                    tag,
                    "동일 TAG의 외부 속성이 일치하지 않습니다: "
                    + ", ".join(conflicts),
                    tuple(_provenance(record) for record in matches),
                )
            )
    if report_unobserved:
        for tag, matches in sorted(indexed.items()):
            if tag not in observed:
                issues.append(
                    CrossSheetValidationIssue(
                        CrossSheetIssueKind.MISSING_FROM_DRAWING,
                        tag,
                        "외부 참조 TAG가 현재 도면 증거에서 발견되지 않았습니다.",
                        tuple(_provenance(record) for record in matches),
                    )
                )
    return CrossSheetValidationReport(observed, len(records), tuple(issues))


def _read_csv_rows(path: Path) -> tuple[tuple[str, ...], ...]:
    raw = path.read_bytes()
    text: str
    for encoding in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("CSV 문자 인코딩은 UTF-8 또는 CP949여야 합니다.")
    return tuple(tuple(row) for row in csv.reader(text.splitlines()))


def _read_xlsx_sheets(path: Path) -> tuple[tuple[str, tuple[tuple[str, ...], ...]], ...]:
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise ValueError("손상된 XLSX 구성 파일이 있습니다.")
            if sum(item.file_size for item in archive.infolist()) > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise ValueError("XLSX 압축 해제 크기가 200MB를 초과했습니다.")
            shared = _shared_strings(archive)
            workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            relationships = _workbook_relationships(archive)
            output = []
            for sheet in workbook.findall(f".//{{{XML_NS}}}sheet"):
                name = sheet.attrib.get("name", "Sheet")
                relationship_id = sheet.attrib.get(f"{{{REL_NS}}}id", "")
                target = relationships.get(relationship_id)
                if not target:
                    continue
                normalized_target = target.replace("\\", "/").lstrip("/").removeprefix("xl/")
                if not normalized_target.startswith("worksheets/") or ".." in normalized_target.split("/"):
                    raise ValueError("XLSX worksheet 관계 경로가 올바르지 않습니다.")
                member = "xl/" + normalized_target
                output.append((name, _worksheet_rows(archive.read(member), shared)))
            return tuple(output)
    except (KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise ValueError(f"XLSX 참조 목록을 읽을 수 없습니다: {error}") from error


def _shared_strings(archive: zipfile.ZipFile) -> tuple[str, ...]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return ()
    return tuple(
        "".join(node.text or "" for node in item.iter(f"{{{XML_NS}}}t"))
        for item in root.findall(f"{{{XML_NS}}}si")
    )


def _workbook_relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    root = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    return {
        item.attrib.get("Id", ""): item.attrib.get("Target", "")
        for item in root.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
    }


def _worksheet_rows(
    content: bytes, shared: tuple[str, ...]
) -> tuple[tuple[str, ...], ...]:
    root = ElementTree.fromstring(content)
    rows: list[tuple[str, ...]] = []
    for row in root.findall(f".//{{{XML_NS}}}row"):
        values: dict[int, str] = {}
        for cell in row.findall(f"{{{XML_NS}}}c"):
            reference = cell.attrib.get("r", "A1")
            column = _column_index(reference)
            cell_type = cell.attrib.get("t")
            value_node = cell.find(f"{{{XML_NS}}}v")
            if cell_type == "inlineStr":
                value = "".join(
                    node.text or "" for node in cell.iter(f"{{{XML_NS}}}t")
                )
            else:
                value = value_node.text if value_node is not None and value_node.text else ""
                if cell_type == "s" and value:
                    index = int(value)
                    value = shared[index] if 0 <= index < len(shared) else ""
            values[column] = value
        width = max(values, default=-1) + 1
        rows.append(tuple(values.get(index, "") for index in range(width)))
    return tuple(rows)


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Za-z]+", reference)
    if not letters:
        return 0
    value = 0
    for character in letters.group(0).upper():
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _find_tag_column(headers: tuple[str, ...], requested: str | None) -> int:
    normalized = tuple(_header_key(header) for header in headers)
    if requested:
        key = _header_key(requested)
        if key not in normalized:
            raise ValueError(f"지정한 TAG 열을 찾을 수 없습니다: {requested}")
        return normalized.index(key)
    matches = [index for index, header in enumerate(normalized) if header in TAG_HEADERS]
    if len(matches) != 1:
        raise ValueError("TAG 열을 하나로 확정할 수 없습니다. tag_column을 지정하세요.")
    return matches[0]


def _conflicting_fields(
    records: list[ExternalTagRecord], fields: tuple[str, ...]
) -> tuple[str, ...]:
    conflicts = []
    for field in fields:
        key = _header_key(field)
        values = {
            value.strip().casefold()
            for record in records
            for header, value in record.attributes
            if _header_key(header) == key and value.strip()
        }
        if len(values) > 1:
            conflicts.append(field)
    return tuple(conflicts)


def _provenance(record: ExternalTagRecord) -> str:
    return f"{record.source_path}#{record.sheet_name}!row={record.row_number}"


def _header_key(value: str) -> str:
    return re.sub(r"[\s_.\-/]+", "", value).casefold()


def _tag_key(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()
