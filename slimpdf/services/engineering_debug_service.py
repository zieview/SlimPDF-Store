"""Read-only debug visualization export for Engineering analysis evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from PIL import Image, ImageDraw

from slimpdf.core.jobs import CancellationToken


MAX_DEBUG_PAGES = 10_000
MAX_PREVIEW_EDGE = 3_000
STATUS_COLORS = {
    "pass": "#16803A",
    "review": "#E00000",
    "auto_correct": "#E67E00",
    "approved": "#1769AA",
}
ProgressCallback = Callable[[int, int], None]


def export_engineering_debug_images(
    source_pdf: Path,
    result_directory: Path,
    output_directory: Path,
    *,
    on_progress: ProgressCallback | None = None,
    cancellation: CancellationToken | None = None,
) -> Path:
    """Render status-colored evidence boxes to a separate immutable directory."""

    import pymupdf

    token = cancellation or CancellationToken()
    source = source_pdf.expanduser().resolve()
    manifest = result_directory.expanduser().resolve() / "engineering_analysis.json"
    target = output_directory.expanduser().resolve()
    if not source.is_file() or not manifest.is_file():
        raise FileNotFoundError("원본 PDF 또는 Engineering 분석 결과를 찾을 수 없습니다.")
    if target.exists():
        raise FileExistsError(f"기존 Debug 결과를 덮어쓸 수 없습니다: {target}")
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Engineering 분석 결과를 읽을 수 없습니다: {error}") from error
    if value.get("schema_version") not in {1, 2}:
        raise ValueError("지원하지 않는 Engineering 분석 schema입니다.")
    if value.get("document", {}).get("source_sha256") != _sha256(source):
        raise ValueError("분석 결과와 원본 PDF의 SHA-256이 일치하지 않습니다.")
    pages = value.get("pages")
    if not isinstance(pages, list) or len(pages) > MAX_DEBUG_PAGES:
        raise ValueError("Debug 출력 페이지 구성이 올바르지 않습니다.")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    temporary.mkdir()
    document = None
    try:
        document = pymupdf.open(source)
        if document.needs_pass:
            raise ValueError("암호화 PDF는 Debug 이미지를 생성할 수 없습니다.")
        if len(pages) != len(document):
            raise ValueError("분석 결과와 원본 PDF의 페이지 수가 일치하지 않습니다.")
        for index, page_value in enumerate(pages):
            token.raise_if_cancelled()
            page_number = int(page_value.get("page_number", 0))
            if page_number != index + 1:
                raise ValueError("분석 결과의 페이지 순서가 올바르지 않습니다.")
            page = document[index]
            scale = min(MAX_PREVIEW_EDGE / max(page.rect.width, page.rect.height), 2.0)
            pixmap = page.get_pixmap(
                matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=False
            )
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            draw = ImageDraw.Draw(image)
            for text in page_value.get("texts", ()):
                status = str(text.get("status", ""))
                if status not in STATUS_COLORS:
                    raise ValueError(f"지원하지 않는 검토 상태입니다: {status}")
                x0, y0, x1, y1 = _valid_box(text.get("bounding_box"))
                if x1 > page.rect.width or y1 > page.rect.height:
                    raise ValueError("Text bounding box가 페이지 범위를 벗어났습니다.")
                draw.rectangle(
                    (x0 * scale, y0 * scale, x1 * scale, y1 * scale),
                    outline=STATUS_COLORS[status],
                    width=max(2, round(scale * 2)),
                )
            image.save(temporary / f"page-{page_number:04d}.png", format="PNG")
            if on_progress:
                on_progress(index + 1, len(pages))
        legend = {
            "schema_version": 1,
            "source_pdf_sha256": _sha256(source),
            "analysis_manifest_sha256": _sha256(manifest),
            "page_count": len(pages),
            "colors": STATUS_COLORS,
            "annotation_rendered": False,
        }
        (temporary / "debug_manifest.json").write_text(
            json.dumps(legend, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        _validate_output(temporary, len(pages))
        token.raise_if_cancelled()
        os.rename(temporary, target)
    finally:
        if document is not None:
            document.close()
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return target


def _valid_box(value: object) -> tuple[float, float, float, float]:
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError) as error:
        raise ValueError("Text bounding box가 올바르지 않습니다.") from error
    if min(x0, y0) < 0 or x1 <= x0 or y1 <= y0:
        raise ValueError("Text bounding box 범위가 올바르지 않습니다.")
    return x0, y0, x1, y1


def _validate_output(directory: Path, page_count: int) -> None:
    images = tuple(directory.glob("page-*.png"))
    if len(images) != page_count or any(path.stat().st_size <= 0 for path in images):
        raise RuntimeError("Engineering Debug 이미지 구성이 올바르지 않습니다.")
    if (directory / "debug_manifest.json").stat().st_size <= 0:
        raise RuntimeError("Engineering Debug manifest가 비어 있습니다.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
