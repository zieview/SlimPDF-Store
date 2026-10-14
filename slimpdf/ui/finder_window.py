"""Focused PDF Finder window; search processing remains in the core layer."""

from __future__ import annotations

import logging
import tkinter as tk
from datetime import datetime
from pathlib import Path
from threading import Thread
from tkinter import filedialog, messagebox, ttk

from slimpdf.core.jobs import CancellationToken
from slimpdf.core.pdf_finder import collect_pdf_paths, search_pdfs
from slimpdf.finder_models import (
    FinderRequest,
    SearchMatchMode,
    SearchTermOperator,
)
from slimpdf.repositories.finder_xlsx_repository import export_finder_result
from slimpdf.repositories.finder_cache_repository import clear_finder_cache
from slimpdf.services.windows_credential_service import (
    CredentialManagerError,
    list_pdf_credential_identifiers,
    read_pdf_credential,
)
from slimpdf.ui.theme import apply_modern_theme
from slimpdf.ui.window_layout import fit_window_to_screen


LOGGER = logging.getLogger(__name__)


class FinderWindow:
    """Collect search options and present page-level results."""

    def __init__(self, parent: tk.Misc, initial_paths: tuple[Path, ...] = ()) -> None:
        self.parent = parent
        self.window = tk.Toplevel(parent)
        self.window.title("SlimPDF PDF FINDER")
        fit_window_to_screen(
            self.window, 1020, 720, minimum_width=760, minimum_height=560
        )
        apply_modern_theme(self.window)
        self.paths = list(initial_paths)
        self.cancellation: CancellationToken | None = None
        self._build()
        self._refresh_file_list()

    def _build(self) -> None:
        outer = ttk.Frame(self.window, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="PDF FINDER", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="최대 500개 PDF에서 검색어를 찾고, 스캔 페이지는 필요할 때만 OCR합니다.",
        ).pack(anchor="w", pady=(2, 14))

        file_frame = ttk.LabelFrame(outer, text="검색 대상", padding=10)
        file_frame.pack(fill="x")
        button_row = ttk.Frame(file_frame)
        button_row.pack(fill="x")
        ttk.Button(button_row, text="PDF 추가", command=self._add_files).pack(side="left")
        ttk.Button(button_row, text="폴더 추가", command=self._add_folder).pack(side="left", padx=6)
        ttk.Button(button_row, text="목록 비우기", command=self._clear_files).pack(side="left")
        ttk.Button(button_row, text="저장 캐시 삭제", command=self._clear_cache).pack(side="left", padx=6)
        self.include_subfolders = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            button_row, text="하위 폴더 포함", variable=self.include_subfolders
        ).pack(side="left", padx=14)
        self.file_label = ttk.Label(file_frame, text="선택된 PDF 0개")
        self.file_label.pack(anchor="w", pady=(8, 0))

        options = ttk.LabelFrame(outer, text="검색 조건", padding=10)
        options.pack(fill="x", pady=12)
        ttk.Label(options, text="검색어 (줄바꿈으로 여러 개 입력)").grid(row=0, column=0, sticky="w")
        self.terms = tk.Text(
            options,
            height=3,
            font=("Segoe UI", 10),
            background="#FFFFFF",
            foreground="#20242C",
            insertbackground="#20242C",
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
        )
        self.terms.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(4, 8))
        options.columnconfigure(0, weight=1)
        ttk.Label(options, text="방식").grid(row=2, column=0, sticky="w")
        self.match_mode = tk.StringVar(value=SearchMatchMode.CONTAINS.value)
        ttk.Combobox(
            options,
            textvariable=self.match_mode,
            state="readonly",
            values=("contains", "exact", "regex"),
            width=12,
        ).grid(row=2, column=1, sticky="w", padx=(5, 15))
        self.operator = tk.StringVar(value=SearchTermOperator.OR.value)
        ttk.Combobox(
            options,
            textvariable=self.operator,
            state="readonly",
            values=("or", "and"),
            width=8,
        ).grid(row=2, column=2, sticky="w")
        self.case_sensitive = tk.BooleanVar(value=False)
        self.normalize_spacing = tk.BooleanVar(value=True)
        self.use_ocr = tk.BooleanVar(value=True)
        self.cache_enabled = tk.BooleanVar(value=False)
        ttk.Checkbutton(options, text="대소문자 구분", variable=self.case_sensitive).grid(row=2, column=3, padx=8)
        ttk.Checkbutton(options, text="한글 띄어쓰기 정규화", variable=self.normalize_spacing).grid(row=2, column=4, padx=8)
        ttk.Checkbutton(options, text="스캔 페이지 OCR", variable=self.use_ocr).grid(row=2, column=5, padx=8)
        ttk.Checkbutton(
            options,
            text="검색 캐시 사용 (본문을 사용자 PC에 저장)",
            variable=self.cache_enabled,
        ).grid(row=3, column=0, columnspan=6, sticky="w", pady=(8, 0))

        credential_row = ttk.Frame(outer)
        credential_row.pack(fill="x", pady=(0, 10))
        ttk.Label(credential_row, text="암호화 PDF 승인 자격 증명").pack(side="left")
        self.credential_identifier = tk.StringVar(value="사용 안 함")
        identifiers = self._credential_identifiers()
        ttk.Combobox(
            credential_row,
            textvariable=self.credential_identifier,
            state="readonly",
            values=("사용 안 함", *identifiers),
            width=28,
        ).pack(side="left", padx=8)
        self.start_button = ttk.Button(credential_row, text="검색 시작", command=self._start)
        self.start_button.pack(side="right")
        self.cancel_button = ttk.Button(credential_row, text="취소", command=self._cancel, state="disabled")
        self.cancel_button.pack(side="right", padx=6)

        result_frame = ttk.LabelFrame(outer, text="검색 결과", padding=8)
        result_frame.pack(fill="both", expand=True)
        columns = ("file", "page", "terms", "ocr", "excerpt")
        self.results = ttk.Treeview(result_frame, columns=columns, show="headings")
        headings = {"file": "파일", "page": "페이지", "terms": "검색어", "ocr": "OCR", "excerpt": "검색어 포함 문장"}
        widths = {"file": 220, "page": 55, "terms": 130, "ocr": 45, "excerpt": 430}
        for column in columns:
            self.results.heading(column, text=headings[column])
            self.results.column(column, width=widths[column], anchor="w")
        self.results.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(result_frame, orient="vertical", command=self.results.yview)
        scrollbar.pack(side="right", fill="y")
        self.results.configure(yscrollcommand=scrollbar.set)
        self.status = ttk.Label(outer, text="검색할 PDF와 검색어를 입력하세요.")
        self.status.pack(fill="x", pady=(10, 0))

    def _credential_identifiers(self) -> tuple[str, ...]:
        try:
            return list_pdf_credential_identifiers()
        except CredentialManagerError:
            LOGGER.warning("Finder could not enumerate approved credentials")
            return ()

    def _add_files(self) -> None:
        selected = filedialog.askopenfilenames(parent=self.window, filetypes=[("PDF files", "*.pdf")])
        self.paths.extend(Path(item) for item in selected)
        self._refresh_file_list()

    def _add_folder(self) -> None:
        selected = filedialog.askdirectory(parent=self.window)
        if selected:
            try:
                self.paths.extend(
                    collect_pdf_paths((Path(selected),), include_subfolders=self.include_subfolders.get())
                )
            except Exception as error:
                messagebox.showerror("PDF FINDER", str(error), parent=self.window)
            self._refresh_file_list()

    def _clear_files(self) -> None:
        self.paths.clear()
        self._refresh_file_list()

    def _clear_cache(self) -> None:
        """Delete user-local Finder text caches after explicit confirmation."""

        if not messagebox.askyesno(
            "PDF FINDER 캐시 삭제",
            "저장된 PDF 본문 검색 캐시를 모두 삭제하시겠습니까?",
            parent=self.window,
        ):
            return
        removed = clear_finder_cache()
        self.status.config(text=f"저장된 Finder 캐시 {removed}개를 삭제했습니다.")

    def _refresh_file_list(self) -> None:
        self.paths = list(dict.fromkeys(self.paths))
        self.file_label.config(text=f"선택된 PDF {len(self.paths)}개")

    def _start(self) -> None:
        terms = tuple(self.terms.get("1.0", "end").splitlines())
        credentials = ()
        identifier = self.credential_identifier.get()
        try:
            if identifier != "사용 안 함":
                credentials = (read_pdf_credential(identifier),)
            request = FinderRequest(
                input_paths=tuple(self.paths),
                search_terms=terms,
                match_mode=SearchMatchMode(self.match_mode.get()),
                operator=SearchTermOperator(self.operator.get()),
                case_sensitive=self.case_sensitive.get(),
                normalize_korean_spacing=self.normalize_spacing.get(),
                use_ocr=self.use_ocr.get(),
                credentials=credentials,
                cache_enabled=self.cache_enabled.get(),
            )
        except Exception as error:
            messagebox.showerror("PDF FINDER", str(error), parent=self.window)
            return
        initial_directory = str(self.paths[0].parent) if self.paths else str(Path.home())
        output_name = f"PDF_FINDER_검색결과_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        selected_output = filedialog.asksaveasfilename(
            parent=self.window,
            title="FINDER 검색 결과 Excel 저장",
            initialdir=initial_directory,
            initialfile=output_name,
            defaultextension=".xlsx",
            filetypes=[("Excel 통합 문서", "*.xlsx")],
        )
        if not selected_output:
            return
        output_path = Path(selected_output)
        self.cancellation = CancellationToken()
        self.start_button.config(state="disabled")
        self.cancel_button.config(state="normal")
        for item in self.results.get_children():
            self.results.delete(item)

        def worker() -> None:
            try:
                result = search_pdfs(
                    request,
                    cancellation=self.cancellation,
                    on_progress=lambda index, total, path, page: self.window.after(
                        0,
                        lambda: self.status.config(
                            text=f"{index + 1}/{total} {path.name} - {page}페이지 분석 중"
                        ),
                    ),
                )
                report_path = export_finder_result(result, output_path)
            except Exception as error:
                LOGGER.exception("PDF Finder search failed")
                self.window.after(0, self._finish_error, error)
            else:
                self.window.after(0, self._finish, result, report_path)

        Thread(target=worker, name="SlimPDF-Finder", daemon=False).start()

    def _cancel(self) -> None:
        if self.cancellation is not None:
            self.cancellation.cancel()
            self.cancel_button.config(state="disabled")
            self.status.config(text="현재 페이지가 끝나는 즉시 취소합니다...")

    def _finish_error(self, error: Exception) -> None:
        self.start_button.config(state="normal")
        self.cancel_button.config(state="disabled")
        messagebox.showerror("PDF FINDER", str(error), parent=self.window)

    def _finish(self, result, report_path: Path) -> None:
        self.start_button.config(state="normal")
        self.cancel_button.config(state="disabled")
        for match in result.matches:
            self.results.insert(
                "",
                "end",
                values=(match.input_path.name, match.page_number, ", ".join(match.matched_terms), "예" if match.used_ocr else "아니오", match.sentence or match.excerpt),
            )
        failures = tuple(item for item in result.diagnostics if item.error)
        low_confidence = sum(len(item.low_confidence_pages) for item in result.diagnostics)
        state = "취소됨" if result.cancelled else "완료"
        self.status.config(
            text=f"{state}: 결과 {len(result.matches)}건, 실패 {len(failures)}개, 낮은 OCR 신뢰도 {low_confidence}페이지 · {report_path.name}"
        )
        if failures:
            details = "\n".join(f"• {item.input_path.name}: {item.error}" for item in failures[:10])
            messagebox.showwarning(
                "PDF FINDER 일부 실패",
                f"{details}\n\n전체 결과는 다음 Excel 파일에 저장했습니다.\n{report_path}",
                parent=self.window,
            )
        else:
            messagebox.showinfo(
                "PDF FINDER 완료",
                f"검색 결과 {len(result.matches)}건을 저장했습니다.\n\n{report_path}",
                parent=self.window,
            )
