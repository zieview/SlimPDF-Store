"""Write PDF Finder results to a dependency-free, standards-compliant XLSX file."""

from __future__ import annotations

import math
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.sax.saxutils import escape
from xml.etree import ElementTree
from uuid import uuid4

from slimpdf.core.compression import publish_output_without_overwrite
from slimpdf.finder_models import FinderMatch, FinderResult


_INVALID_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
_POINT_TO_MM = 25.4 / 72.0
_HEADERS = (
    "번호",
    "파일명",
    "전체 경로",
    "페이지",
    "페이지 내 위치",
    "X (mm)",
    "Y (mm)",
    "폭 (mm)",
    "높이 (mm)",
    "검색어",
    "검색어 포함 문장",
    "OCR",
)
_WIDTHS = (7, 28, 58, 9, 18, 11, 11, 11, 11, 28, 70, 9)


def export_finder_result(result: FinderResult, output_path: Path) -> Path:
    """Create an XLSX report without replacing an existing user file."""

    target = output_path.expanduser().resolve()
    if target.suffix.casefold() != ".xlsx":
        target = target.with_suffix(".xlsx")
    if target.exists():
        raise FileExistsError(f"기존 파일을 덮어쓸 수 없습니다: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.stem}.{uuid4().hex}.tmp.xlsx")
    try:
        _write_workbook(temporary, result)
        if temporary.stat().st_size <= 0:
            raise RuntimeError("FINDER Excel 결과가 비어 있습니다.")
        _validate_workbook(temporary)
        publish_output_without_overwrite(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _validate_workbook(path: Path) -> None:
    """Reject incomplete or malformed OOXML before publishing it to the user."""

    required_parts = {
        "[Content_Types].xml",
        "_rels/.rels",
        "xl/workbook.xml",
        "xl/_rels/workbook.xml.rels",
        "xl/styles.xml",
        "xl/worksheets/sheet1.xml",
    }
    try:
        with zipfile.ZipFile(path) as archive:
            corrupted = archive.testzip()
            if corrupted:
                raise RuntimeError(f"손상된 XLSX 구성 파일: {corrupted}")
            missing = required_parts.difference(archive.namelist())
            if missing:
                raise RuntimeError(
                    "XLSX 필수 구성 파일이 누락되었습니다: " + ", ".join(sorted(missing))
                )
            for name in archive.namelist():
                if name.endswith((".xml", ".rels")):
                    ElementTree.fromstring(archive.read(name))
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as error:
        raise RuntimeError(f"FINDER Excel 결과 무결성 검증에 실패했습니다: {error}") from error


def _write_workbook(path: Path, result: FinderResult) -> None:
    failures = tuple(item for item in result.diagnostics if item.error)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types(bool(failures)))
        archive.writestr("_rels/.rels", _package_relationships())
        archive.writestr("docProps/core.xml", _core_properties())
        archive.writestr("docProps/app.xml", _app_properties(bool(failures)))
        archive.writestr("xl/workbook.xml", _workbook(bool(failures)))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_relationships(bool(failures)))
        archive.writestr("xl/styles.xml", _styles())
        rows = [_HEADERS]
        rows.extend(_match_row(index, match) for index, match in enumerate(result.matches, 1))
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            _worksheet(
                rows,
                _WIDTHS,
                numeric_columns={1, 4, 6, 7, 8, 9},
                decimal_columns={6, 7, 8, 9},
            ),
        )
        if failures:
            error_rows = [("번호", "파일명", "전체 경로", "오류 내용")]
            error_rows.extend(
                (index, item.input_path.name, str(item.input_path), item.error or "")
                for index, item in enumerate(failures, 1)
            )
            archive.writestr(
                "xl/worksheets/sheet2.xml",
                _worksheet(error_rows, (7, 30, 65, 80), numeric_columns={1}),
            )


def _match_row(index: int, match: FinderMatch) -> tuple[object, ...]:
    rectangle = getattr(match, "rectangle", None)
    if rectangle:
        x0, y0, x1, y1 = rectangle
        coordinates: tuple[object, ...] = (
            round(x0 * _POINT_TO_MM, 1),
            round(y0 * _POINT_TO_MM, 1),
            round(max(0.0, x1 - x0) * _POINT_TO_MM, 1),
            round(max(0.0, y1 - y0) * _POINT_TO_MM, 1),
        )
    else:
        coordinates = ("", "", "", "")
    return (
        index,
        match.input_path.name,
        str(match.input_path),
        match.page_number,
        _position_label(match),
        *coordinates,
        ", ".join(match.matched_terms),
        getattr(match, "sentence", "") or match.excerpt,
        "예" if match.used_ocr else "아니오",
    )


def _position_label(match: FinderMatch) -> str:
    rectangle = getattr(match, "rectangle", None)
    page_size = getattr(match, "page_size", None)
    if not rectangle or not page_size:
        return "위치 확인 불가"
    x0, y0, x1, y1 = rectangle
    page_width, page_height = page_size
    if page_width <= 0 or page_height <= 0:
        return "위치 확인 불가"
    x_ratio = ((x0 + x1) / 2) / page_width
    y_ratio = ((y0 + y1) / 2) / page_height
    horizontal = "좌측" if x_ratio < 1 / 3 else "우측" if x_ratio > 2 / 3 else "중앙"
    vertical = "상단" if y_ratio < 1 / 3 else "하단" if y_ratio > 2 / 3 else "중앙"
    return f"{vertical} {horizontal}"


def _worksheet(
    rows,
    widths,
    *,
    numeric_columns: set[int],
    decimal_columns: set[int] | None = None,
) -> str:
    decimal_columns = decimal_columns or set()
    maximum_column = len(widths)
    maximum_row = max(1, len(rows))
    column_xml = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, 1)
    )
    row_xml: list[str] = []
    for row_index, row in enumerate(rows, 1):
        height = 28 if row_index == 1 else 42
        cells = "".join(
            _cell(
                row_index,
                column_index,
                value,
                header=row_index == 1,
                numeric=column_index in numeric_columns,
                decimal=column_index in decimal_columns,
            )
            for column_index, value in enumerate(row, 1)
        )
        row_xml.append(f'<row r="{row_index}" ht="{height}" customHeight="1">{cells}</row>')
    final_column = _column_name(maximum_column)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>'
        f'<dimension ref="A1:{final_column}{maximum_row}"/>'
        '<sheetViews><sheetView showGridLines="0" workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="15"/>'
        f'<cols>{column_xml}</cols><sheetData>{"".join(row_xml)}</sheetData>'
        f'<autoFilter ref="A1:{final_column}{maximum_row}"/>'
        '<pageMargins left="0.25" right="0.25" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
        '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0" paperSize="9"/>'
        '</worksheet>'
    )


def _cell(
    row: int,
    column: int,
    value: object,
    *,
    header: bool,
    numeric: bool,
    decimal: bool,
) -> str:
    reference = f"{_column_name(column)}{row}"
    style = 1 if header else 2 if decimal and isinstance(value, (int, float)) else 0
    if numeric and isinstance(value, (int, float)) and math.isfinite(float(value)):
        return f'<c r="{reference}" s="{style}"><v>{value}</v></c>'
    text = _xml_text(str(value))
    return f'<c r="{reference}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _xml_text(value: str) -> str:
    return escape(_INVALID_XML.sub("", value))


def _content_types(has_errors: bool) -> str:
    second = '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' if has_errors else ""
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' + second + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>'


def _package_relationships() -> str:
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>'


def _workbook(has_errors: bool) -> str:
    error_sheet = '<sheet name="처리 오류" sheetId="2" r:id="rId2"/>' if has_errors else ""
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><bookViews><workbookView/></bookViews><sheets><sheet name="검색 결과" sheetId="1" r:id="rId1"/>' + error_sheet + '</sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>'


def _workbook_relationships(has_errors: bool) -> str:
    error_rel = '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>' if has_errors else ""
    styles_id = 3 if has_errors else 2
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' + error_rel + f'<Relationship Id="rId{styles_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'


def _styles() -> str:
    border = '<border><left style="thin"><color rgb="FF808080"/></left><right style="thin"><color rgb="FF808080"/></right><top style="thin"><color rgb="FF808080"/></top><bottom style="thin"><color rgb="FF808080"/></bottom><diagonal/></border>'
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="0.0"/></numFmts><fonts count="2"><font><sz val="10"/><name val="맑은 고딕"/><family val="2"/><charset val="129"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="맑은 고딕"/><family val="2"/><charset val="129"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFC85A7C"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>' + border + '</borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf numFmtId="0" fontId="1" fillId="2" borderId="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="1" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'


def _core_properties() -> str:
    stamp = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:creator>SlimPDF</dc:creator><cp:lastModifiedBy>SlimPDF</cp:lastModifiedBy><dc:title>PDF FINDER 검색 결과</dc:title><dcterms:created xsi:type="dcterms:W3CDTF">' + stamp + '</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">' + stamp + '</dcterms:modified></cp:coreProperties>'


def _app_properties(has_errors: bool) -> str:
    names = '<vt:lpstr>검색 결과</vt:lpstr>' + ('<vt:lpstr>처리 오류</vt:lpstr>' if has_errors else '')
    count = 2 if has_errors else 1
    return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>SlimPDF</Application><HeadingPairs><vt:vector size="2" baseType="variant"><vt:variant><vt:lpstr>워크시트</vt:lpstr></vt:variant><vt:variant><vt:i4>' + str(count) + '</vt:i4></vt:variant></vt:vector></HeadingPairs><TitlesOfParts><vt:vector size="' + str(count) + '" baseType="lpstr">' + names + '</vt:vector></TitlesOfParts></Properties>'
