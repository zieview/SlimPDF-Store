"""Read-only visual review window for structured engineering analysis."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from threading import Thread
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageDraw, ImageTk

from slimpdf.core.jobs import CancellationToken, JobCancelledError
from slimpdf.engineering_ocr_models import (
    EngineeringFeedbackAction,
    EngineeringFeedbackDecision,
    EngineeringValueKind,
)
from slimpdf.services.engineering_debug_service import export_engineering_debug_images
from slimpdf.services.engineering_feedback_service import (
    export_feedback_draft,
    load_feedback_draft,
)
from slimpdf.ui.theme import apply_modern_theme
from slimpdf.ui.window_layout import fit_window_to_screen


LOGGER = logging.getLogger(__name__)
MAX_REVIEW_ITEMS = 200_000
MAX_NUMERICAL_ISSUES = 200_000
MAX_VALUES_PER_TEXT = 10_000


def next_debug_output_directory(parent: Path, source_stem: str) -> Path:
    """Choose a new debug directory without replacing an earlier export."""

    root = parent.expanduser().resolve()
    if not root.is_dir():
        raise ValueError("Debug 결과 상위 폴더를 찾을 수 없습니다.")
    base = root / f"{source_stem}_engineering_debug"
    if not base.exists():
        return base
    for sequence in range(2, 10_001):
        candidate = root / f"{source_stem}_engineering_debug_{sequence}"
        if not candidate.exists():
            return candidate
    raise FileExistsError("사용 가능한 Engineering Debug 결과 이름이 없습니다.")


@dataclass(frozen=True, slots=True)
class EngineeringReviewDisplayItem:
    text_id: str
    page_number: int
    raw_text: str
    bounding_box: tuple[float, float, float, float]
    status: str
    fused_confidence: float
    reasons: tuple[str, ...]
    candidates: tuple[str, ...]
    external_issues: tuple[str, ...]
    engineering_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class NumericalReviewDisplayItem:
    entity_key: str
    value_kind: str
    unit: str
    observed_values: tuple[str, ...]
    provenance: tuple[str, ...]


def load_review_items(
    source_pdf: Path, result_directory: Path
) -> tuple[EngineeringReviewDisplayItem, ...]:
    """Validate a result manifest against its source and return REVIEW items."""

    value = _load_validated_manifest(source_pdf, result_directory)
    items: list[EngineeringReviewDisplayItem] = []
    for page in value.get("pages", ()):
        page_number = int(page.get("page_number", 0))
        for text in page.get("texts", ()):
            if str(text.get("status", "")) != "review":
                continue
            if len(items) >= MAX_REVIEW_ITEMS:
                raise ValueError("검토 항목이 200,000개를 초과했습니다.")
            box = _valid_box(text.get("bounding_box"))
            candidates = tuple(
                f"{item.get('raw_text', '')} → {item.get('suggested_text', '')}"
                for item in text.get("correction_candidates", ())
            )
            issues = tuple(
                f"{item.get('message', '')} ({item.get('tag', '')})"
                for item in text.get("external_issues", ())
            )
            values = _display_engineering_values(
                str(text.get("raw_text", "")),
                text.get("engineering_values", ()),
            )
            items.append(
                EngineeringReviewDisplayItem(
                    str(text.get("text_id", "")),
                    page_number,
                    str(text.get("raw_text", "")),
                    box,
                    "review",
                    float(text.get("fused_confidence", 0.0)),
                    tuple(str(reason) for reason in text.get("reasons", ())),
                    candidates,
                    issues,
                    values,
                )
            )
    return tuple(items)


def _display_engineering_values(raw_text: str, value: object) -> tuple[str, ...]:
    """Validate schema-2 typed values and format them for read-only review."""

    if value in (None, ()):
        return ()
    if not isinstance(value, list):
        raise ValueError("Engineering value 목록 형식이 올바르지 않습니다.")
    if len(value) > MAX_VALUES_PER_TEXT:
        raise ValueError("한 text의 Engineering value가 10,000개를 초과했습니다.")
    allowed_kinds = {item.value for item in EngineeringValueKind}
    displays = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Engineering value 형식이 올바르지 않습니다.")
        try:
            start = int(item.get("start"))
            end = int(item.get("end"))
        except (TypeError, ValueError) as error:
            raise ValueError("Engineering value 문자 범위가 올바르지 않습니다.") from error
        raw = str(item.get("raw_text", ""))
        kind = str(item.get("kind", "")).strip()
        normalized = str(item.get("normalized_text", "")).strip()
        value_text = str(item.get("value_text", "")).strip()
        unit = str(item.get("unit") or "").strip()
        if (
            not raw
            or kind not in allowed_kinds
            or not normalized
            or not value_text
            or start < 0
            or end <= start
            or end > len(raw_text)
            or raw_text[start:end] != raw
        ):
            raise ValueError("Engineering value 원문 범위가 분석 text와 일치하지 않습니다.")
        suffix = f", 단위 {unit}" if unit else ""
        displays.append(f"{kind}: {raw} → {normalized}{suffix} [{start}:{end}]")
    return tuple(displays)


def load_numerical_review_issues(
    source_pdf: Path, result_directory: Path
) -> tuple[NumericalReviewDisplayItem, ...]:
    """Return validated document-level numerical REVIEW issues."""

    value = _load_validated_manifest(source_pdf, result_directory)
    if "numerical_issues" not in value:
        return ()
    rows = value["numerical_issues"]
    if not isinstance(rows, list) or len(rows) > MAX_NUMERICAL_ISSUES:
        raise ValueError("수치 충돌 목록 형식이 올바르지 않습니다.")
    issues = []
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "conflicting_value":
            raise ValueError("지원하지 않는 수치 충돌 형식입니다.")
        entity = str(row.get("entity_key", "")).strip()
        value_kind = str(row.get("value_kind", "")).strip()
        values = row.get("observed_values")
        provenance = row.get("provenance")
        if (
            not entity
            or not value_kind
            or not isinstance(values, list)
            or len(values) < 2
            or not all(isinstance(item, str) and item for item in values)
            or not isinstance(provenance, list)
            or len(provenance) != len(values)
            or not all(isinstance(item, str) and item for item in provenance)
        ):
            raise ValueError("수치 충돌 증거가 올바르지 않습니다.")
        issues.append(
            NumericalReviewDisplayItem(
                entity,
                value_kind,
                str(row.get("unit") or ""),
                tuple(values),
                tuple(provenance),
            )
        )
    return tuple(issues)


def _load_validated_manifest(source_pdf: Path, result_directory: Path) -> dict:
    source = source_pdf.expanduser().resolve()
    manifest_path = result_directory.expanduser().resolve() / "engineering_analysis.json"
    if not source.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("원본 PDF 또는 Engineering 분석 결과를 찾을 수 없습니다.")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Engineering 분석 결과를 읽을 수 없습니다: {error}") from error
    if not isinstance(value, dict) or value.get("schema_version") not in {1, 2}:
        raise ValueError("지원하지 않는 Engineering 분석 schema입니다.")
    expected_hash = str(value.get("document", {}).get("source_sha256", ""))
    if not expected_hash or _sha256(source) != expected_hash:
        raise ValueError("분석 결과와 원본 PDF의 SHA-256이 일치하지 않습니다.")
    return value


def render_review_preview(
    source_pdf: Path,
    item: EngineeringReviewDisplayItem,
    maximum_size: tuple[int, int],
) -> Image.Image:
    """Render one bounded page preview and highlight the source-coordinate box."""

    import pymupdf

    max_width, max_height = maximum_size
    if max_width < 100 or max_height < 100:
        raise ValueError("미리보기 크기가 너무 작습니다.")
    with pymupdf.open(source_pdf) as document:
        if document.needs_pass:
            raise ValueError("암호화 PDF는 미리보기를 표시할 수 없습니다.")
        if item.page_number <= 0 or item.page_number > len(document):
            raise ValueError("검토 항목의 페이지 번호가 PDF 범위를 벗어났습니다.")
        page = document[item.page_number - 1]
        scale = min(max_width / page.rect.width, max_height / page.rect.height, 2.0)
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), alpha=False, annots=False
        )
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    x0, y0, x1, y1 = item.bounding_box
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (x0 * scale, y0 * scale, x1 * scale, y1 * scale),
        outline="#E00000",
        width=max(2, round(scale * 2)),
    )
    return image


class EngineeringReviewWindow:
    """Show review items and bounded page previews without editing the PDF."""

    def __init__(self, parent: tk.Misc, source_pdf: Path, result_directory: Path) -> None:
        self.source_pdf = source_pdf
        self.result_directory = result_directory
        self.items = load_review_items(source_pdf, result_directory)
        self.numerical_issues = load_numerical_review_issues(
            source_pdf, result_directory
        )
        self.window = tk.Toplevel(parent)
        self.window.title("SlimPDF Engineering 검토")
        self.window.configure(background="#FFFFFF")
        fit_window_to_screen(
            self.window, 1180, 820, minimum_width=820, minimum_height=600
        )
        apply_modern_theme(self.window)
        self.preview_photo: ImageTk.PhotoImage | None = None
        self._preview_serial = 0
        self.decisions: dict[str, EngineeringFeedbackDecision] = {}
        self.debug_token: CancellationToken | None = None
        self._build()
        if self.items:
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)
            self.tree.focus(first)
            self._select_item()

    def _build(self) -> None:
        outer = ttk.Frame(self.window, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer, text="Engineering 검토", font=("Segoe UI", 18, "bold")
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="원본 PDF는 변경하지 않습니다. 빨간 사각형은 검토 위치 미리보기입니다.",
        ).pack(anchor="w", pady=(2, 12))
        body = ttk.Panedwindow(outer, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body, padding=(0, 0, 8, 0))
        right = ttk.Frame(body, padding=(8, 0, 0, 0))
        body.add(left, weight=2)
        body.add(right, weight=3)
        self.tree = ttk.Treeview(
            left,
            columns=("page", "text", "confidence", "reason"),
            show="headings",
            selectmode="browse",
        )
        headings = {
            "page": "페이지", "text": "원문", "confidence": "신뢰도", "reason": "검토 사유"
        }
        widths = {"page": 65, "text": 180, "confidence": 75, "reason": 210}
        for name, label in headings.items():
            self.tree.heading(name, text=label)
            self.tree.column(name, width=widths[name], anchor="w")
        scrollbar = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        for index, item in enumerate(self.items):
            self.tree.insert(
                "", "end", iid=str(index),
                values=(
                    item.page_number,
                    item.raw_text,
                    f"{item.fused_confidence * 100:.1f}%",
                    ", ".join(item.reasons),
                ),
            )
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._select_item())

        self.canvas = tk.Canvas(
            right, background="#FFFFFF", highlightbackground="#E4E7EC", highlightthickness=1
        )
        self.canvas.pack(fill="both", expand=True)
        self.detail = tk.Text(
            right,
            height=7,
            wrap="word",
            font=("맑은 고딕", 10),
            background="#FFFFFF",
            foreground="#20242C",
            relief="solid",
            borderwidth=1,
            padx=10,
            pady=8,
        )
        self.detail.pack(fill="x", pady=(10, 0))
        self.detail.configure(state="disabled")
        feedback = ttk.LabelFrame(right, text="검토 결정 (선택 사항)", padding=8)
        feedback.pack(fill="x", pady=(8, 0))
        self.corrected_var = tk.StringVar()
        ttk.Entry(feedback, textvariable=self.corrected_var).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(feedback, text="교정 등록", command=self._mark_corrected).pack(
            side="right", padx=(6, 0)
        )
        ttk.Button(feedback, text="원문 승인", command=self._accept_raw).pack(
            side="right", padx=(6, 0)
        )
        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(12, 0))
        self.summary_label = ttk.Label(
            actions,
            text=(
                f"텍스트 검토 {len(self.items)}건 · 수치 충돌 "
                f"{len(self.numerical_issues)}건 · 결정 0건"
            ),
        )
        self.summary_label.pack(side="left")
        ttk.Button(
            actions,
            text="수치 충돌 보기",
            command=self._show_numerical_issues,
            state="normal" if self.numerical_issues else "disabled",
        ).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="결과 폴더 열기", command=self._open_result).pack(side="right")
        ttk.Button(actions, text="닫기", command=self._close).pack(side="right", padx=6)
        ttk.Button(actions, text="검토 초안 저장", command=self._save_draft).pack(
            side="right", padx=6
        )
        ttk.Button(actions, text="검토 초안 불러오기", command=self._load_draft).pack(
            side="right", padx=6
        )
        self.debug_cancel_button = ttk.Button(
            actions, text="Debug 취소", command=self._cancel_debug, state="disabled"
        )
        self.debug_cancel_button.pack(side="right", padx=6)
        self.debug_button = ttk.Button(
            actions, text="Debug 내보내기", command=self._start_debug_export
        )
        self.debug_button.pack(side="right", padx=6)
        self.debug_status_label = ttk.Label(outer, text="")
        self.debug_status_label.pack(fill="x", pady=(6, 0))
        self.window.protocol("WM_DELETE_WINDOW", self._close)

    def _select_item(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        item = self.items[int(selection[0])]
        previous = self.decisions.get(item.text_id)
        self.corrected_var.set(
            previous.corrected_text if previous is not None else item.raw_text
        )
        detail = [f"원문: {item.raw_text}", f"검토 사유: {', '.join(item.reasons) or '없음'}"]
        if item.candidates:
            detail.append("교정 후보: " + "; ".join(item.candidates))
        if item.external_issues:
            detail.append("외부 목록 이슈: " + "; ".join(item.external_issues))
        if item.engineering_values:
            detail.append("Engineering 수치: " + "; ".join(item.engineering_values))
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "\n".join(detail))
        self.detail.configure(state="disabled")
        self._preview_serial += 1
        serial = self._preview_serial
        self.canvas.delete("all")
        self.canvas.create_text(20, 20, text="미리보기 준비 중...", anchor="nw")

        def worker() -> None:
            try:
                image = render_review_preview(self.source_pdf, item, (760, 560))
            except Exception as error:
                self.window.after(0, self._show_preview_error, serial, error)
                return
            self.window.after(0, self._show_preview, serial, image)

        Thread(target=worker, name="SlimPDF-Engineering-Preview", daemon=True).start()

    def _selected_item(self) -> EngineeringReviewDisplayItem | None:
        selection = self.tree.selection()
        return self.items[int(selection[0])] if selection else None

    def _accept_raw(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        self.decisions[item.text_id] = EngineeringFeedbackDecision(
            item.text_id, EngineeringFeedbackAction.ACCEPT_RAW, item.raw_text
        )
        self.corrected_var.set(item.raw_text)
        self._update_decision_summary()

    def _mark_corrected(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        corrected = self.corrected_var.get().strip()
        if not corrected or corrected == item.raw_text:
            messagebox.showwarning(
                "Engineering 검토",
                "교정 문자열은 비어 있지 않아야 하며 원문과 달라야 합니다.",
                parent=self.window,
            )
            return
        self.decisions[item.text_id] = EngineeringFeedbackDecision(
            item.text_id, EngineeringFeedbackAction.CORRECT, corrected
        )
        self._update_decision_summary()

    def _update_decision_summary(self) -> None:
        self.summary_label.config(
            text=(
                f"텍스트 검토 {len(self.items)}건 · 수치 충돌 "
                f"{len(self.numerical_issues)}건 · 결정 {len(self.decisions)}건"
            )
        )

    def _show_numerical_issues(self) -> None:
        if not self.numerical_issues:
            return
        dialog = tk.Toplevel(self.window)
        dialog.title("SlimPDF Engineering 수치 충돌")
        dialog.configure(background="#FFFFFF")
        dialog.transient(self.window)
        fit_window_to_screen(dialog, 900, 520, minimum_width=680, minimum_height=420)
        apply_modern_theme(dialog)
        outer = ttk.Frame(dialog, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer, text="수치 일관성 REVIEW", font=("Segoe UI", 15, "bold")
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text="동일 TAG·종류·단위에서 서로 다른 값이 발견됐습니다. 자동 수정하지 않습니다.",
        ).pack(anchor="w", pady=(2, 10))
        tree = ttk.Treeview(
            outer,
            columns=("entity", "kind", "unit", "values", "source"),
            show="headings",
        )
        columns = (
            ("entity", "TAG", 140),
            ("kind", "종류", 130),
            ("unit", "단위", 70),
            ("values", "관측값", 180),
            ("source", "출처", 300),
        )
        for name, label, width in columns:
            tree.heading(name, text=label)
            tree.column(name, width=width, anchor="w")
        for index, issue in enumerate(self.numerical_issues):
            tree.insert(
                "", "end", iid=str(index),
                values=(
                    issue.entity_key, issue.value_kind, issue.unit,
                    " / ".join(issue.observed_values),
                    " | ".join(issue.provenance),
                ),
            )
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="left", fill="y")
        ttk.Button(outer, text="닫기", command=dialog.destroy).pack(
            side="right", anchor="s", padx=(10, 0)
        )

    def _save_draft(self) -> None:
        if not self.decisions:
            messagebox.showwarning(
                "Engineering 검토", "먼저 검토 결정을 등록하세요.", parent=self.window
            )
            return
        suggested = f"{self.source_pdf.stem}_engineering_feedback.json"
        selected = filedialog.asksaveasfilename(
            parent=self.window,
            title="Engineering 검토 초안 저장",
            initialdir=str(self.result_directory.parent),
            initialfile=suggested,
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if not selected:
            return
        try:
            destination = export_feedback_draft(
                self.source_pdf,
                self.result_directory,
                tuple(self.decisions.values()),
                Path(selected),
            )
        except Exception as error:
            LOGGER.exception("Engineering feedback draft export failed")
            messagebox.showerror(
                "검토 초안 저장 실패", str(error), parent=self.window
            )
            return
        messagebox.showinfo(
            "Engineering 검토",
            f"검토 초안을 저장했습니다.\n\n{destination}\n\n"
            "이 파일은 학습 데이터가 아니며 별도 승인 전에는 자동 반영되지 않습니다.",
            parent=self.window,
        )

    def _load_draft(self) -> None:
        if self.decisions and not messagebox.askyesno(
            "Engineering 검토",
            "현재 등록한 결정을 검토 초안의 내용으로 바꾸시겠습니까?",
            parent=self.window,
        ):
            return
        selected = filedialog.askopenfilename(
            parent=self.window,
            title="Engineering 검토 초안 불러오기",
            initialdir=str(self.result_directory.parent),
            filetypes=[("JSON", "*.json")],
        )
        if not selected:
            return
        try:
            loaded = load_feedback_draft(
                self.source_pdf, self.result_directory, Path(selected)
            )
        except Exception as error:
            LOGGER.warning("Engineering feedback draft load failed: %s", error)
            messagebox.showerror(
                "검토 초안 불러오기 실패", str(error), parent=self.window
            )
            return
        self.decisions = {item.text_id: item for item in loaded}
        self._update_decision_summary()
        self._select_item()
        messagebox.showinfo(
            "Engineering 검토",
            f"검토 결정 {len(loaded)}건을 불러왔습니다.",
            parent=self.window,
        )

    def _start_debug_export(self) -> None:
        if self.debug_token is not None:
            return
        selected = filedialog.askdirectory(
            parent=self.window,
            title="Engineering Debug 결과 상위 폴더 선택",
            initialdir=str(self.result_directory.parent),
        )
        if not selected:
            return
        try:
            target = next_debug_output_directory(Path(selected), self.source_pdf.stem)
        except Exception as error:
            messagebox.showerror("Debug 내보내기 실패", str(error), parent=self.window)
            return
        token = CancellationToken()
        self.debug_token = token
        self.debug_button.config(state="disabled")
        self.debug_cancel_button.config(state="normal")
        self.debug_status_label.config(text="Debug 이미지 준비 중...")

        def progress(completed: int, total: int) -> None:
            self._schedule_debug_callback(
                self.debug_status_label.config,
                {"text": f"Debug 이미지 생성 중... {completed}/{total}"},
            )

        def worker() -> None:
            try:
                output = export_engineering_debug_images(
                    self.source_pdf,
                    self.result_directory,
                    target,
                    on_progress=progress,
                    cancellation=token,
                )
            except JobCancelledError:
                self._schedule_debug_callback(
                    self._finish_debug_export, None, None, True
                )
            except Exception as error:
                LOGGER.exception("Engineering Debug export failed")
                self._schedule_debug_callback(
                    self._finish_debug_export, None, error, False
                )
            else:
                self._schedule_debug_callback(
                    self._finish_debug_export, output, None, False
                )

        Thread(target=worker, name="SlimPDF-Engineering-Debug", daemon=True).start()

    def _schedule_debug_callback(self, callback, *args) -> None:
        """Ignore late worker callbacks after the review window has closed."""

        try:
            self.window.after(
                0,
                callback,
                *args,
            )
        except (RuntimeError, tk.TclError):
            LOGGER.debug("Ignored late Engineering Debug UI callback")

    def _cancel_debug(self) -> None:
        if self.debug_token is not None:
            self.debug_token.cancel()
            self.debug_cancel_button.config(state="disabled")
            self.debug_status_label.config(text="현재 페이지가 끝나면 취소합니다...")

    def _finish_debug_export(
        self, output: Path | None, error: Exception | None, cancelled: bool
    ) -> None:
        if not self.window.winfo_exists():
            return
        self.debug_token = None
        self.debug_button.config(state="normal")
        self.debug_cancel_button.config(state="disabled")
        if cancelled:
            self.debug_status_label.config(text="Debug 이미지 생성을 취소했습니다.")
            return
        if error is not None:
            self.debug_status_label.config(text="Debug 이미지 생성 실패")
            messagebox.showerror("Debug 내보내기 실패", str(error), parent=self.window)
            return
        self.debug_status_label.config(text=f"Debug 이미지 생성 완료: {output}")
        if output is not None and messagebox.askyesno(
            "Engineering Debug", "Debug 결과 폴더를 여시겠습니까?", parent=self.window
        ):
            try:
                opener = getattr(os, "startfile", None)
                if opener is None:
                    raise RuntimeError("결과 폴더 열기는 Windows에서만 지원됩니다.")
                opener(str(output))
            except Exception as open_error:
                messagebox.showerror(
                    "결과 폴더 열기 실패", str(open_error), parent=self.window
                )

    def _close(self) -> None:
        if self.debug_token is not None:
            self.debug_token.cancel()
        self.window.destroy()

    def _show_preview(self, serial: int, image: Image.Image) -> None:
        if serial != self._preview_serial or not self.window.winfo_exists():
            return
        self.preview_photo = ImageTk.PhotoImage(image, master=self.window)
        self.canvas.delete("all")
        self.canvas.create_image(
            max(0, (self.canvas.winfo_width() - image.width) // 2),
            max(0, (self.canvas.winfo_height() - image.height) // 2),
            image=self.preview_photo,
            anchor="nw",
        )

    def _show_preview_error(self, serial: int, error: Exception) -> None:
        if serial != self._preview_serial or not self.window.winfo_exists():
            return
        LOGGER.warning("Engineering review preview failed: %s", error)
        self.canvas.delete("all")
        self.canvas.create_text(20, 20, text=f"미리보기 실패: {error}", anchor="nw")

    def _open_result(self) -> None:
        try:
            opener = getattr(os, "startfile", None)
            if opener is None:
                raise RuntimeError("결과 폴더 열기는 Windows에서만 지원됩니다.")
            opener(str(self.result_directory))
        except Exception as error:
            messagebox.showerror("결과 폴더 열기 실패", str(error), parent=self.window)


def _valid_box(value: object) -> tuple[float, float, float, float]:
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError) as error:
        raise ValueError("검토 항목 좌표가 올바르지 않습니다.") from error
    if x1 <= x0 or y1 <= y0 or min(x0, y0) < 0:
        raise ValueError("검토 항목 좌표 범위가 올바르지 않습니다.")
    return x0, y0, x1, y1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
