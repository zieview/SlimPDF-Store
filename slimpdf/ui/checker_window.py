"""Interactive PDF Checker setup and exclusion preview window."""

from __future__ import annotations

import logging
import os
import tkinter as tk
from pathlib import Path
from threading import Thread
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageTk

from slimpdf.checker_models import CheckerRequest, NormalizedRect
from slimpdf.core.jobs import CancellationToken
from slimpdf.core.pdf_checker import compare_pdf_revisions
from slimpdf.repositories.checker_preset_repository import (
    list_checker_presets,
    load_checker_preset,
    save_checker_preset,
)
from slimpdf.services.pdf_service import pdf_has_signatures
from slimpdf.ui.theme import apply_modern_theme
from slimpdf.ui.window_layout import fit_window_to_screen


LOGGER = logging.getLogger(__name__)


def format_checker_path(path: Path) -> str:
    """Keep the filename visible while omitting long intermediate directories."""

    return f"…\\{path.name}"


def open_checked_pdf(path: Path) -> None:
    """Open a completed CHECKER result with the Windows default PDF viewer."""

    if not path.is_file():
        raise FileNotFoundError(f"CHECKER 결과 PDF를 찾을 수 없습니다: {path}")
    os.startfile(str(path), "open")


class CheckerWindow:
    """Preview latest revision and run one cancellable comparison."""

    def __init__(self, parent: tk.Misc, paths: tuple[Path, Path]) -> None:
        self.parent = parent
        self.previous_path, self.latest_path = paths
        self.window = tk.Toplevel(parent)
        self.window.title("SlimPDF PDF CHECKER")
        fit_window_to_screen(
            self.window, 1040, 800, minimum_width=780, minimum_height=600
        )
        apply_modern_theme(self.window)
        self.exclusions: list[NormalizedRect] = []
        self.drag_start: tuple[int, int] | None = None
        self.preview_image = None
        self.preview_scale = (1.0, 1.0)
        self.cancellation: CancellationToken | None = None
        self._build()
        self._refresh_paths()
        self._render_preview()

    def _build(self) -> None:
        outer = ttk.Frame(self.window, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="PDF CHECKER", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="이전 Revision과 최신 Revision을 비교해 변경 영역을 최신 PDF 사본에 표시합니다.",
        ).pack(anchor="w", pady=(2, 12))

        path_frame = ttk.LabelFrame(outer, text="Revision 순서 확인", padding=10)
        path_frame.pack(fill="x")
        self.previous_label = ttk.Label(path_frame)
        self.previous_label.grid(row=0, column=0, sticky="w")
        self.latest_label = ttk.Label(path_frame)
        self.latest_label.grid(row=1, column=0, sticky="w", pady=(5, 0))
        ttk.Button(path_frame, text="이전/최신 바꾸기", command=self._swap_paths).grid(row=0, column=1, rowspan=2, padx=10)
        path_frame.columnconfigure(0, weight=1)

        option_frame = ttk.Frame(outer)
        option_frame.pack(fill="x", pady=10)
        ttk.Label(option_frame, text="REV. NO.").pack(side="left")
        self.revision = ttk.Entry(option_frame, width=16)
        self.revision.pack(side="left", padx=6)
        ttk.Label(option_frame, text="제외 영역 프리셋").pack(side="left", padx=(18, 4))
        self.preset_name = tk.StringVar(value="")
        self.preset_combo = ttk.Combobox(
            option_frame,
            textvariable=self.preset_name,
            state="readonly",
            values=list_checker_presets(),
            width=22,
        )
        self.preset_combo.pack(side="left")
        ttk.Button(option_frame, text="불러오기", command=self._load_preset).pack(side="left", padx=4)
        ttk.Button(option_frame, text="현재 영역 저장", command=self._save_preset).pack(side="left")

        preview_frame = ttk.LabelFrame(
            outer,
            text="최신 도면 첫 페이지 미리보기 - 드래그: 제외 영역 추가 / 우클릭: 영역 삭제",
            padding=8,
        )
        preview_frame.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(
            preview_frame,
            background="#FFFFFF",
            cursor="cross",
            highlightbackground="#E4E7EC",
            highlightthickness=1,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._drag_begin)
        self.canvas.bind("<ButtonRelease-1>", self._drag_end)
        self.canvas.bind("<Button-3>", self._remove_region)
        self.canvas.bind("<Configure>", lambda _event: self._render_preview())

        action_frame = ttk.Frame(outer)
        action_frame.pack(fill="x", pady=(10, 0))
        self.status = ttk.Label(action_frame, text="제외 영역 0개")
        self.status.pack(side="left")
        self.progress = ttk.Progressbar(
            action_frame,
            mode="determinate",
            maximum=100,
            value=0,
            length=280,
        )
        self.progress.pack(side="left", padx=(14, 0))
        self.start_button = ttk.Button(action_frame, text="CHECK 시작", command=self._start)
        self.start_button.pack(side="right")
        self.cancel_button = ttk.Button(action_frame, text="취소", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=6)

    def _refresh_paths(self) -> None:
        self.previous_label.config(
            text=f"이전 Revision: {format_checker_path(self.previous_path)}"
        )
        self.latest_label.config(
            text=f"최신 Revision: {format_checker_path(self.latest_path)}"
        )

    def _swap_paths(self) -> None:
        self.previous_path, self.latest_path = self.latest_path, self.previous_path
        self._refresh_paths()
        self._render_preview()

    def _render_preview(self) -> None:
        width = max(300, self.canvas.winfo_width() - 20)
        height = max(260, self.canvas.winfo_height() - 20)
        try:
            import pymupdf

            with pymupdf.open(self.latest_path) as document:
                if document.needs_pass:
                    raise ValueError("암호화 PDF는 먼저 보안 해제하세요.")
                pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2), alpha=False, annots=True)
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            image.thumbnail((width, height), Image.Resampling.LANCZOS)
            self.preview_image = ImageTk.PhotoImage(image)
            self.canvas.delete("all")
            x = (self.canvas.winfo_width() - image.width) // 2
            y = (self.canvas.winfo_height() - image.height) // 2
            self.canvas.create_image(x, y, image=self.preview_image, anchor="nw", tags="preview")
            self.preview_bounds = (x, y, x + image.width, y + image.height)
            self._draw_exclusions()
        except Exception as error:
            LOGGER.warning("Checker preview failed: %s", error)
            self.canvas.delete("all")
            self.canvas.create_text(20, 20, anchor="nw", text=str(error), fill="#B42318")

    def _draw_exclusions(self) -> None:
        if not hasattr(self, "preview_bounds"):
            return
        x0, y0, x1, y1 = self.preview_bounds
        width, height = x1 - x0, y1 - y0
        for index, rectangle in enumerate(self.exclusions):
            self.canvas.create_rectangle(
                x0 + rectangle.x0 * width,
                y0 + rectangle.y0 * height,
                x0 + rectangle.x1 * width,
                y0 + rectangle.y1 * height,
                outline="#7F56D9",
                fill="#D6BBFB",
                stipple="gray25",
                width=2,
                tags=("exclusion", f"exclusion-{index}"),
            )

    def _drag_begin(self, event) -> None:
        self.drag_start = (event.x, event.y)

    def _drag_end(self, event) -> None:
        if self.drag_start is None or not hasattr(self, "preview_bounds"):
            return
        start_x, start_y = self.drag_start
        self.drag_start = None
        bx0, by0, bx1, by1 = self.preview_bounds
        x0, x1 = sorted((max(bx0, min(bx1, start_x)), max(bx0, min(bx1, event.x))))
        y0, y1 = sorted((max(by0, min(by1, start_y)), max(by0, min(by1, event.y))))
        if x1 - x0 < 8 or y1 - y0 < 8:
            return
        self.exclusions.append(
            NormalizedRect(
                (x0 - bx0) / (bx1 - bx0),
                (y0 - by0) / (by1 - by0),
                (x1 - bx0) / (bx1 - bx0),
                (y1 - by0) / (by1 - by0),
            )
        )
        self._render_preview()
        self.status.config(text=f"제외 영역 {len(self.exclusions)}개")

    def _remove_region(self, event) -> None:
        if not hasattr(self, "preview_bounds"):
            return
        bx0, by0, bx1, by1 = self.preview_bounds
        nx = (event.x - bx0) / max(1, bx1 - bx0)
        ny = (event.y - by0) / max(1, by1 - by0)
        for index in range(len(self.exclusions) - 1, -1, -1):
            rectangle = self.exclusions[index]
            if rectangle.x0 <= nx <= rectangle.x1 and rectangle.y0 <= ny <= rectangle.y1:
                self.exclusions.pop(index)
                self._render_preview()
                self.status.config(text=f"제외 영역 {len(self.exclusions)}개")
                break

    def _load_preset(self) -> None:
        try:
            self.exclusions = list(load_checker_preset(self.preset_name.get()))
            self._render_preview()
            self.status.config(text=f"제외 영역 {len(self.exclusions)}개")
        except Exception as error:
            messagebox.showerror("PDF CHECKER", str(error), parent=self.window)

    def _save_preset(self) -> None:
        name = simpledialog.askstring("CHECKER 프리셋", "프리셋 이름", parent=self.window)
        if not name:
            return
        try:
            save_checker_preset(name, tuple(self.exclusions))
            self.preset_combo.config(values=list_checker_presets())
            self.preset_name.set(name.strip())
        except Exception as error:
            messagebox.showerror("PDF CHECKER", str(error), parent=self.window)

    def _start(self) -> None:
        revision = self.revision.get().strip()
        allow_signature = False
        if pdf_has_signatures(self.latest_path):
            allow_signature = messagebox.askyesno(
                "전자서명 무효화 경고",
                "CHECK 주석을 추가하면 최신 PDF 사본의 전자서명이 무효화됩니다.\n원본은 변경하지 않습니다. 계속하시겠습니까?",
                parent=self.window,
            )
            if not allow_signature:
                return
        request = CheckerRequest(
            self.previous_path,
            self.latest_path,
            revision,
            tuple(self.exclusions),
            allow_signature,
        )
        self.cancellation = CancellationToken()
        self.start_button.config(state="disabled")
        self.cancel_button.config(state="normal")
        self.progress.config(value=0)
        self.status.config(text="페이지 자동 대응 준비 중...")

        def progress(stage: str, current: int, total: int) -> None:
            self.window.after(0, self._update_progress, stage, current, total)

        def worker() -> None:
            try:
                result = compare_pdf_revisions(
                    request,
                    cancellation=self.cancellation,
                    progress=progress,
                )
            except Exception as error:
                LOGGER.exception("PDF Checker failed")
                self.window.after(0, self._finish_error, error)
            else:
                self.window.after(0, self._finish, result)

        Thread(target=worker, name="SlimPDF-Checker", daemon=False).start()

    def _update_progress(self, stage: str, current: int, total: int) -> None:
        ratio = min(1.0, max(0.0, current / max(1, total)))
        if stage == "matching":
            percent = ratio * 25
            text = f"페이지 자동 대응 중... {current}/{total}"
        elif stage == "comparing":
            percent = 25 + ratio * 60
            text = f"페이지 비교 중... {current}/{total}"
        elif stage == "saving":
            percent = 85 + ratio * 3
            text = "결과 PDF 저장 중..."
        elif stage == "validating":
            percent = 88 + ratio * 6
            text = "결과 PDF 무결성 검증 중..."
        elif stage == "publishing":
            percent = 94 + ratio * 5
            text = "결과 파일 게시 중..."
        else:
            percent = 100
            text = "CHECK 완료"
        self.progress.config(value=round(percent, 1))
        self.status.config(text=text)

    def _cancel(self) -> None:
        if self.cancellation:
            self.cancellation.cancel()
            self.cancel_button.config(state="disabled")
            self.status.config(text="현재 페이지가 끝나는 즉시 취소합니다...")

    def _finish_error(self, error: Exception) -> None:
        self.start_button.config(state="normal")
        self.cancel_button.config(state="disabled")
        self.progress.config(value=0)
        self.status.config(text="비교 실패")
        messagebox.showerror("PDF CHECKER", str(error), parent=self.window)

    def _finish(self, result) -> None:
        self.start_button.config(state="normal")
        self.cancel_button.config(state="disabled")
        self.progress.config(value=100)
        self.status.config(text=f"완료: 변경 영역 {len(result.changes)}개")
        messagebox.showinfo(
            "PDF CHECKER 완료",
            f"변경 영역 {len(result.changes)}개를 표시했습니다.\n결과: {result.output_path}",
            parent=self.window,
        )
        try:
            open_checked_pdf(result.output_path)
        except OSError as error:
            LOGGER.warning("Unable to open completed Checker PDF: %s", error)
            messagebox.showwarning(
                "PDF CHECKER",
                f"결과 PDF는 정상적으로 생성됐지만 자동으로 열지 못했습니다.\n"
                f"결과: {result.output_path}\n상세: {error}",
                parent=self.window,
            )
