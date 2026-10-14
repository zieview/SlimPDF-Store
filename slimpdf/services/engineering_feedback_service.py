"""Validated, non-destructive export of Engineering review decisions."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from slimpdf.engineering_ocr_models import (
    EngineeringFeedbackAction,
    EngineeringFeedbackDecision,
)


MAX_FEEDBACK_ITEMS = 200_000
MAX_CORRECTED_TEXT_LENGTH = 10_000
MAX_FEEDBACK_BYTES = 50 * 1024 * 1024


def export_feedback_draft(
    source_pdf: Path,
    result_directory: Path,
    decisions: tuple[EngineeringFeedbackDecision, ...],
    output_path: Path,
) -> Path:
    """Publish a review draft without changing the PDF or analysis manifest."""

    source = source_pdf.expanduser().resolve()
    manifest = result_directory.expanduser().resolve() / "engineering_analysis.json"
    destination = output_path.expanduser().resolve()
    if not source.is_file() or not manifest.is_file():
        raise FileNotFoundError("원본 PDF 또는 Engineering 분석 결과를 찾을 수 없습니다.")
    if destination.suffix.casefold() != ".json":
        raise ValueError("Engineering 검토 초안은 JSON 파일로 저장해야 합니다.")
    if destination.exists():
        raise FileExistsError(f"기존 검토 초안을 덮어쓸 수 없습니다: {destination}")
    if not decisions:
        raise ValueError("저장할 검토 결정이 없습니다.")
    if len(decisions) > MAX_FEEDBACK_ITEMS:
        raise ValueError("검토 결정이 200,000개를 초과했습니다.")

    try:
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Engineering 분석 결과를 읽을 수 없습니다: {error}") from error
    if manifest_value.get("schema_version") not in {1, 2}:
        raise ValueError("지원하지 않는 Engineering 분석 schema입니다.")
    source_hash = _sha256(source)
    if manifest_value.get("document", {}).get("source_sha256") != source_hash:
        raise ValueError("분석 결과와 원본 PDF의 SHA-256이 일치하지 않습니다.")
    review_texts = {
        str(text.get("text_id", "")): str(text.get("raw_text", ""))
        for page in manifest_value.get("pages", ())
        for text in page.get("texts", ())
        if text.get("status") == "review"
    }
    rows = []
    seen: set[str] = set()
    for decision in decisions:
        if not isinstance(decision.action, EngineeringFeedbackAction):
            raise ValueError("지원하지 않는 검토 결정 종류입니다.")
        text_id = decision.text_id.strip()
        corrected = decision.corrected_text.strip()
        if not text_id or text_id not in review_texts:
            raise ValueError(f"REVIEW 항목이 아닌 text_id입니다: {decision.text_id}")
        if text_id in seen:
            raise ValueError(f"중복된 검토 결정입니다: {text_id}")
        if not corrected or len(corrected) > MAX_CORRECTED_TEXT_LENGTH:
            raise ValueError("교정 문자열은 1~10,000자여야 합니다.")
        raw = review_texts[text_id]
        if decision.action is EngineeringFeedbackAction.ACCEPT_RAW and corrected != raw:
            raise ValueError("원문 승인 결정의 문자열은 원문과 일치해야 합니다.")
        if decision.action is EngineeringFeedbackAction.CORRECT and corrected == raw:
            raise ValueError("교정 결정은 원문과 다른 문자열이어야 합니다.")
        seen.add(text_id)
        rows.append(
            {
                "text_id": text_id,
                "action": decision.action.value,
                "raw_text": raw,
                "corrected_text": corrected,
            }
        )
    payload = {
        "schema_version": 1,
        "state": "draft",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "source_pdf_sha256": source_hash,
        "analysis_manifest_sha256": _sha256(manifest),
        "decisions": rows,
    }
    content = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".slimpdf-feedback-", suffix=".tmp",
            dir=destination.parent, delete=False
        ) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        os.rename(temporary, destination)  # Windows: fails if destination exists.
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


def load_feedback_draft(
    source_pdf: Path,
    result_directory: Path,
    draft_path: Path,
) -> tuple[EngineeringFeedbackDecision, ...]:
    """Load a draft only when both source and analysis hashes still match."""

    source = source_pdf.expanduser().resolve()
    manifest = result_directory.expanduser().resolve() / "engineering_analysis.json"
    draft = draft_path.expanduser().resolve()
    if not source.is_file() or not manifest.is_file() or not draft.is_file():
        raise FileNotFoundError("원본 PDF, 분석 결과 또는 검토 초안을 찾을 수 없습니다.")
    if draft.suffix.casefold() != ".json":
        raise ValueError("Engineering 검토 초안은 JSON 파일이어야 합니다.")
    if draft.stat().st_size > MAX_FEEDBACK_BYTES:
        raise ValueError("Engineering 검토 초안은 50MB를 초과할 수 없습니다.")
    try:
        value = json.loads(draft.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Engineering 검토 초안을 읽을 수 없습니다: {error}") from error
    if value.get("schema_version") != 1 or value.get("state") != "draft":
        raise ValueError("지원하지 않는 Engineering 검토 초안입니다.")
    if value.get("source_pdf_sha256") != _sha256(source):
        raise ValueError("검토 초안과 원본 PDF의 SHA-256이 일치하지 않습니다.")
    if value.get("analysis_manifest_sha256") != _sha256(manifest):
        raise ValueError("검토 초안과 분석 결과의 SHA-256이 일치하지 않습니다.")

    try:
        manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Engineering 분석 결과를 읽을 수 없습니다: {error}") from error
    if manifest_value.get("schema_version") not in {1, 2}:
        raise ValueError("지원하지 않는 Engineering 분석 schema입니다.")
    review_texts = {
        str(text.get("text_id", "")): str(text.get("raw_text", ""))
        for page in manifest_value.get("pages", ())
        for text in page.get("texts", ())
        if text.get("status") == "review"
    }
    rows = value.get("decisions")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_FEEDBACK_ITEMS:
        raise ValueError("검토 초안의 결정 목록이 올바르지 않습니다.")
    decisions: list[EngineeringFeedbackDecision] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("검토 초안의 결정 형식이 올바르지 않습니다.")
        text_id = str(row.get("text_id", "")).strip()
        raw = str(row.get("raw_text", ""))
        corrected = str(row.get("corrected_text", "")).strip()
        try:
            action = EngineeringFeedbackAction(str(row.get("action", "")))
        except ValueError as error:
            raise ValueError("지원하지 않는 검토 결정 종류입니다.") from error
        if text_id in seen or text_id not in review_texts:
            raise ValueError(f"검토 초안의 text_id가 올바르지 않습니다: {text_id}")
        if raw != review_texts[text_id]:
            raise ValueError(f"검토 초안의 원문이 분석 결과와 다릅니다: {text_id}")
        if not corrected or len(corrected) > MAX_CORRECTED_TEXT_LENGTH:
            raise ValueError("교정 문자열은 1~10,000자여야 합니다.")
        if action is EngineeringFeedbackAction.ACCEPT_RAW and corrected != raw:
            raise ValueError("원문 승인 결정의 문자열은 원문과 일치해야 합니다.")
        if action is EngineeringFeedbackAction.CORRECT and corrected == raw:
            raise ValueError("교정 결정은 원문과 다른 문자열이어야 합니다.")
        seen.add(text_id)
        decisions.append(EngineeringFeedbackDecision(text_id, action, corrected))
    return tuple(decisions)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
