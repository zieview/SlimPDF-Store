"""Offline graph projection of already exported Engineering evidence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path


MAX_MANIFEST_BYTES = 100 * 1024 * 1024


def export_engineering_graph(
    source_pdf: Path, analysis_directory: Path, destination: Path
) -> Path:
    """Export only existing text-symbol links; never infer new graph edges."""

    source = source_pdf.expanduser().resolve()
    manifest_path = analysis_directory.expanduser().resolve() / "engineering_analysis.json"
    output = destination.expanduser().resolve()
    if not source.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("원본 PDF 또는 Engineering 분석 결과를 찾을 수 없습니다.")
    if output.suffix.casefold() != ".json":
        raise ValueError("Engineering graph 출력은 JSON 파일이어야 합니다.")
    if output.exists():
        raise FileExistsError(f"기존 graph 결과를 덮어쓸 수 없습니다: {output}")
    if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("Engineering 분석 결과가 100MB를 초과했습니다.")
    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") not in {1, 2}:
        raise ValueError("지원하지 않는 Engineering 분석 schema입니다.")
    source_hash = _sha256(source)
    if value.get("document", {}).get("source_sha256") != source_hash:
        raise ValueError("분석 결과와 원본 PDF의 SHA-256이 일치하지 않습니다.")

    document_id = str(value.get("document", {}).get("document_id", ""))
    if not document_id:
        raise ValueError("Engineering document ID가 없습니다.")
    nodes: list[dict[str, object]] = [{"id": document_id, "type": "document"}]
    edges: list[dict[str, object]] = []
    node_ids = {document_id}
    edge_ids: set[str] = set()
    pages = value.get("pages")
    if not isinstance(pages, list):
        raise ValueError("Engineering page 목록이 올바르지 않습니다.")
    for page in pages:
        page_id = str(page.get("page_id", ""))
        _add_node(nodes, node_ids, page_id, "page")
        _add_edge(edges, edge_ids, f"{page_id}:belongs_to", page_id, document_id, "BELONGS_TO")
        text_ids = set()
        for text in page.get("texts", ()):
            text_id = str(text.get("text_id", ""))
            _add_node(nodes, node_ids, text_id, "text")
            text_ids.add(text_id)
            _add_edge(edges, edge_ids, f"{text_id}:belongs_to", text_id, page_id, "BELONGS_TO")
        for relation in page.get("relationships", ()):
            text_id = str(relation.get("text_id", ""))
            symbol_id = str(relation.get("symbol_id", ""))
            relationship_id = str(relation.get("relationship_id", ""))
            if text_id not in text_ids or not symbol_id or not relationship_id:
                raise ValueError("관계가 현재 페이지의 text 또는 symbol을 가리키지 않습니다.")
            try:
                score = float(relation.get("score"))
            except (TypeError, ValueError) as error:
                raise ValueError("관계 score가 올바르지 않습니다.") from error
            if not 0 <= score <= 1:
                raise ValueError("관계 score는 0~1 범위여야 합니다.")
            graph_symbol_id = f"{page_id}:symbol:{symbol_id}"
            if graph_symbol_id not in node_ids:
                _add_node(nodes, node_ids, graph_symbol_id, "symbol_reference")
                _add_edge(
                    edges, edge_ids, f"{graph_symbol_id}:belongs_to",
                    graph_symbol_id, page_id, "BELONGS_TO",
                )
            _add_edge(
                edges, edge_ids, relationship_id, text_id, graph_symbol_id,
                "LABELS", score=score,
            )
    payload = {
        "schema_version": 1,
        "source_pdf_sha256": source_hash,
        "analysis_manifest_sha256": _sha256(manifest_path),
        "inference_performed": False,
        "nodes": nodes,
        "edges": edges,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=".slimpdf-graph-", suffix=".tmp",
            dir=output.parent, delete=False,
        ) as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.rename(temporary, output)  # Windows: no overwrite when destination exists.
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return output


def _add_node(nodes: list[dict[str, object]], seen: set[str], node_id: str, kind: str) -> None:
    if not node_id or node_id in seen:
        raise ValueError(f"Engineering graph node ID가 없거나 중복됐습니다: {node_id}")
    seen.add(node_id)
    nodes.append({"id": node_id, "type": kind})


def _add_edge(
    edges: list[dict[str, object]], seen: set[str], edge_id: str,
    source: str, target: str, kind: str, *, score: float | None = None,
) -> None:
    if not edge_id or edge_id in seen:
        raise ValueError(f"Engineering graph edge ID가 없거나 중복됐습니다: {edge_id}")
    seen.add(edge_id)
    item: dict[str, object] = {"id": edge_id, "source": source, "target": target, "type": kind}
    if score is not None:
        item["score"] = score
    edges.append(item)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
