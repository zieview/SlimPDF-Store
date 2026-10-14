"""Main Tkinter window for SlimPDF."""

import logging
import ctypes
import sys
import tkinter as tk
from pathlib import Path
from collections.abc import Sequence
from tkinter import PhotoImage, filedialog, messagebox, simpledialog, ttk
from typing import Any
from threading import Thread
from uuid import uuid4

from PIL import Image, ImageTk

from slimpdf.config import (
    APP_NAME,
    APP_VERSION,
    CARD_BACKGROUND,
    COPYRIGHT_TEXT,
    HIGHLIGHT_COLOR,
    ICON_FILE_NAME,
    LOGO_FILE_NAME,
    STATUS_BACKGROUND,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    WINDOW_BACKGROUND,
    WINDOW_GEOMETRY,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_TITLE,
    WINDOWS_APP_USER_MODEL_ID,
    get_log_file,
)
from slimpdf.core.compression import compress_pdf
from slimpdf.core.engineering_analysis import analyze_engineering_pdf
from slimpdf.core.pdf_unlock import unlock_pdf
from slimpdf.core.jobs import CancellationToken, run_sequential_job
from slimpdf.core.ocr_analysis import analyze_ocr
from slimpdf.core.page_organizer import organize_pdf_pages
from slimpdf.engineering_ocr_models import EngineeringReferenceSelection
from slimpdf.edition import is_news_enabled
from slimpdf.models import CompressionMode, CompressionRequest, CompressionResult
from slimpdf.ocr_models import OcrRequest, OcrResult
from slimpdf.page_models import PageOrganizationMode, PageOrganizationRequest
from slimpdf.security_models import PdfCredential, UnlockRequest, UnlockResult
from slimpdf.services.pdf_security_service import PdfPasswordRequiredError
from slimpdf.services.pdf_service import pdf_has_signatures
from slimpdf.services.engineering_reference_service import load_reference_selections
from slimpdf.ui.dialogs import request_unlock_credentials
from slimpdf.ui.theme import apply_modern_theme
from slimpdf.ui.window_layout import fit_window_to_screen, show_readable_message
from slimpdf.utils.paths import get_app_data_directory, get_resource_path

LOGGER = logging.getLogger(__name__)
ERROR_DETAIL_LIMIT = 2000


class RoundedButton(tk.Canvas):
    """Keyboard-accessible Canvas button with consistently rounded corners."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        text: str,
        command,
        background: str,
        hover_background: str,
        canvas_background: str,
        width: int = 220,
        height: int = 48,
        radius: int = 14,
        font: tuple = ("Segoe UI", 10, "bold"),
    ) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            background=canvas_background,
            highlightthickness=0,
            borderwidth=0,
            cursor="hand2",
            takefocus=1,
        )
        self._text = text
        self._command = command
        self._normal_background = background
        self._hover_background = hover_background
        self._canvas_background = canvas_background
        self._radius = radius
        self._font = font
        self._state = "normal"
        self._hovered = False
        self.bind("<Configure>", self._redraw)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonRelease-1>", self._invoke)
        self.bind("<Return>", self._invoke)
        self.bind("<space>", self._invoke)

    def _rounded_points(self, width: int, height: int) -> list[int]:
        radius = min(self._radius, width // 2, height // 2)
        return [
            radius, 1, width - radius, 1, width - 1, 1, width - 1, radius,
            width - 1, height - radius, width - 1, height - 1,
            width - radius, height - 1, radius, height - 1, 1, height - 1,
            1, height - radius, 1, radius, 1, 1, radius, 1,
        ]

    def _redraw(self, _event=None) -> None:
        self.delete("all")
        width = max(2, self.winfo_width())
        height = max(2, self.winfo_height())
        if self._state == "disabled":
            fill, text_color, outline = "#F2F4F7", "#98A2B3", "#E4E7EC"
        else:
            fill = self._hover_background if self._hovered else self._normal_background
            text_color, outline = TEXT_PRIMARY, "#F08FA0"
        self.create_polygon(
            self._rounded_points(width, height),
            smooth=True,
            splinesteps=24,
            fill=fill,
            outline=outline,
            width=1,
        )
        self.create_text(
            width // 2,
            height // 2,
            text=self._text,
            fill=text_color,
            font=self._font,
        )

    def _on_enter(self, _event=None) -> None:
        if self._state != "disabled":
            self._hovered = True
            self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._hovered = False
        self._redraw()

    def _invoke(self, _event=None) -> None:
        if self._state != "disabled":
            self.focus_set()
            self._command()

    def configure(self, cnf=None, **kwargs):
        """Support the state option used by the application busy-state controller."""

        if cnf:
            kwargs.update(cnf)
        state = kwargs.pop("state", None)
        if state is not None:
            self._state = str(state)
            super().configure(cursor="arrow" if self._state == "disabled" else "hand2")
            self._redraw()
        if kwargs:
            return super().configure(**kwargs)
        return None

    config = configure

    def set_selected(self, selected: bool) -> None:
        """Switch between the selected pink and quiet pink mode treatment."""

        self._normal_background = HIGHLIGHT_COLOR if selected else "#FFF4F6"
        self._hover_background = "#FFA6B4" if selected else "#FFE3E8"
        self._font = ("Segoe UI", 10, "bold" if selected else "normal")
        self._redraw()


def _exception_details(error: Exception) -> str:
    """Return a concise exception cause chain suitable for a GUI dialog."""

    details: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen and len(details) < 4:
        seen.add(id(current))
        message = str(current).strip() or "상세 메시지가 없습니다."
        details.append(f"{type(current).__name__}: {message}")
        current = current.__cause__ or current.__context__
    text = "\n원인: ".join(details)
    if len(text) > ERROR_DETAIL_LIMIT:
        return text[:ERROR_DETAIL_LIMIT].rstrip() + "\n…(나머지 내용은 로그에서 확인하세요.)"
    return text


def build_compression_error_message(
    error: Exception,
    file_path: Path,
    mode: CompressionMode,
    diagnostic_id: str,
) -> str:
    """Build actionable details while leaving the full traceback in the log."""

    log_path = get_log_file(get_app_data_directory())
    return (
        "PDF 압축 작업에 실패했습니다.\n\n"
        f"진단 ID: {diagnostic_id}\n"
        f"작업 모드: {mode.value}\n"
        f"입력 파일: {file_path}\n"
        f"오류 상세: {_exception_details(error)}\n\n"
        f"전체 오류 기록: {log_path}"
    )


def parse_drop_paths(root: tk.Misc, drop_data: str) -> tuple[Path, ...]:
    """Parse tkinterdnd2 data using Tcl's list parser."""

    return tuple(Path(value) for value in root.tk.splitlist(drop_data))


def _available_engineering_output_directory(parent: Path, source_stem: str) -> Path:
    """Choose a new result-directory name without touching existing folders."""

    base = parent / f"{source_stem}_engineering"
    candidate = base
    counter = 2
    while candidate.exists():
        candidate = parent / f"{base.name}_{counter}"
        counter += 1
    return candidate


def create_root() -> tuple[tk.Tk, Any | None]:
    """Create a drag-and-drop root when available, otherwise use Tk."""

    configure_windows_app_identity()
    try:
        import tkinterdnd2 as tkdnd
    except ImportError:
        LOGGER.warning("tkinterdnd2 is unavailable; drag and drop is disabled")
        return tk.Tk(), None
    return tkdnd.TkinterDnD.Tk(), tkdnd


def configure_windows_app_identity() -> None:
    """Give Windows a stable identity so the taskbar uses the SlimPDF icon."""

    if sys.platform != "win32":
        return
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SetCurrentProcessExplicitAppUserModelID.argtypes = (ctypes.c_wchar_p,)
        shell32.SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long
        result = shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_USER_MODEL_ID)
        if result != 0:
            LOGGER.warning("Windows AppUserModelID setup returned HRESULT 0x%08X", result)
    except (AttributeError, OSError):
        LOGGER.exception("Windows AppUserModelID could not be configured")


class SlimPdfApp:
    """Own UI state and connect user events to the compression use case."""

    def __init__(
        self,
        root: tk.Tk,
        dnd_module: Any | None,
        initial_mode: CompressionMode = CompressionMode.MAXIMUM,
    ) -> None:
        self.root = root
        self.dnd_module = dnd_module
        self.compression_mode = initial_mode
        self.logo_image: PhotoImage | None = None
        self.header_logo_image: ImageTk.PhotoImage | None = None
        self.style: ttk.Style
        self.status_label: tk.Label
        self.file_button: RoundedButton
        self.unlock_button: RoundedButton
        self.ocr_button: RoundedButton
        self.engineering_button: RoundedButton
        self.finder_button: RoundedButton
        self.news_button: RoundedButton | None = None
        self.checker_button: RoundedButton
        self.page_organizer_button: RoundedButton
        self.cancel_button: RoundedButton
        self.cancellation_token: CancellationToken | None = None
        self.is_compressing = False
        self._configure_window()
        self._build_widgets()
        self._configure_drag_and_drop()
        self.root.protocol("WM_DELETE_WINDOW", self._handle_close_request)

    def _configure_window(self) -> None:
        self.root.title(WINDOW_TITLE)
        window_width, window_height = map(int, WINDOW_GEOMETRY.split("x", 1))
        fit_window_to_screen(
            self.root,
            window_width,
            window_height,
            minimum_width=WINDOW_MIN_WIDTH,
            minimum_height=min(WINDOW_MIN_HEIGHT, 620),
        )
        self.root.configure(bg=WINDOW_BACKGROUND)
        self.style = apply_modern_theme(self.root)
        self.style.configure("App.TFrame", background=WINDOW_BACKGROUND)
        self.style.configure("Card.TFrame", background=CARD_BACKGROUND)
        self.style.configure(
            "Title.TLabel",
            background=WINDOW_BACKGROUND,
            foreground=TEXT_PRIMARY,
            font=("Segoe UI", 22, "bold"),
        )
        self.style.configure(
            "Subtitle.TLabel",
            background=WINDOW_BACKGROUND,
            foreground=TEXT_SECONDARY,
            font=("Segoe UI", 10),
        )
        self.style.configure(
            "Section.TLabel",
            background=CARD_BACKGROUND,
            foreground=TEXT_PRIMARY,
            font=("Segoe UI", 11, "bold"),
        )
        self.style.configure(
            "Hint.TLabel",
            background=CARD_BACKGROUND,
            foreground=TEXT_SECONDARY,
            font=("Segoe UI", 10),
        )
        icon_path = get_resource_path(ICON_FILE_NAME)
        try:
            self.root.iconbitmap(default=str(icon_path))
            LOGGER.info("Window icon resource loaded: %s", icon_path)
        except (tk.TclError, OSError):
            LOGGER.exception("Window icon resource could not be loaded: %s", icon_path)

        logo_path = get_resource_path(LOGO_FILE_NAME)
        try:
            self.logo_image = PhotoImage(file=logo_path)
            self.root.iconphoto(False, self.logo_image)
            with Image.open(logo_path) as source_logo:
                header_logo = source_logo.convert("RGBA")
                header_logo.thumbnail((48, 48), Image.Resampling.LANCZOS)
                self.header_logo_image = ImageTk.PhotoImage(
                    header_logo,
                    master=self.root,
                )
            LOGGER.info("Logo resource loaded: %s", logo_path)
        except (tk.TclError, OSError, ValueError):
            LOGGER.exception("Logo resource could not be loaded: %s", logo_path)

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(30, 24, 30, 18))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 18))
        if self.header_logo_image is not None:
            ttk.Label(
                header,
                image=self.header_logo_image,
                style="Subtitle.TLabel",
            ).pack(side="left", padx=(0, 12))
        header_text = ttk.Frame(header, style="App.TFrame")
        header_text.pack(side="left", fill="x", expand=True)
        ttk.Label(header_text, text="SlimPDF", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header_text,
            text="PDF 용량은 가볍게, 문서 확인은 안전하게",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        card = ttk.Frame(outer, style="Card.TFrame", padding=(26, 22))
        card.pack(fill="both", expand=True)
        ttk.Label(card, text="PDF 압축", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            card,
            text="PDF 용량을 줄이면서 결과 문서의 무결성을 검사합니다.",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(3, 12))

        self.file_button = RoundedButton(
            card,
            text="PDF 압축",
            command=self.select_file,
            background=HIGHLIGHT_COLOR,
            hover_background="#FFA6B4",
            canvas_background=CARD_BACKGROUND,
            width=540,
            height=52,
            radius=16,
            font=("Segoe UI", 11, "bold"),
        )
        self.file_button.pack(fill="x", pady=(0, 10))
        self.unlock_button = RoundedButton(
            card,
            text="PDF 보안 해제",
            command=self.select_unlock_files,
            background="#FFF4F6",
            hover_background="#FFE3E8",
            canvas_background=CARD_BACKGROUND,
            width=540,
            height=48,
            radius=16,
            font=("Segoe UI", 10, "bold"),
        )
        self.unlock_button.pack(fill="x", pady=(0, 10))
        self.ocr_button = RoundedButton(
            card,
            text="PDF OCR 분석",
            command=self.select_ocr_files,
            background="#FFF4F6",
            hover_background="#FFE3E8",
            canvas_background=CARD_BACKGROUND,
            width=540,
            height=48,
            radius=16,
            font=("Segoe UI", 10, "bold"),
        )
        self.ocr_button.pack(fill="x", pady=(0, 10))
        self.engineering_button = RoundedButton(
            card,
            text="Engineering 도면 분석",
            command=self.select_engineering_pdf,
            background="#FFF4F6",
            hover_background="#FFE3E8",
            canvas_background=CARD_BACKGROUND,
            width=540,
            height=44,
            radius=15,
            font=("Segoe UI", 10, "bold"),
        )
        self.engineering_button.pack(fill="x", pady=(0, 8))
        self.finder_button = RoundedButton(
            card,
            text="PDF FINDER",
            command=self.select_finder_files,
            background="#FFF4F6",
            hover_background="#FFE3E8",
            canvas_background=CARD_BACKGROUND,
            width=540,
            height=48,
            radius=16,
            font=("Segoe UI", 10, "bold"),
        )
        self.finder_button.pack(fill="x", pady=(0, 10))
        if is_news_enabled():
            self.news_button = RoundedButton(
                card,
                text="조선해양 NEWS",
                command=self.open_news,
                background="#FFF4F6",
                hover_background="#FFE3E8",
                canvas_background=CARD_BACKGROUND,
                width=540,
                height=48,
                radius=16,
                font=("Segoe UI", 10, "bold"),
            )
            self.news_button.pack(fill="x", pady=(0, 10))
        self.checker_button = RoundedButton(
            card,
            text="PDF CHECKER",
            command=self.select_checker_files,
            background="#FFF4F6",
            hover_background="#FFE3E8",
            canvas_background=CARD_BACKGROUND,
            width=540,
            height=48,
            radius=16,
            font=("Segoe UI", 10, "bold"),
        )
        self.checker_button.pack(fill="x", pady=(0, 10))
        self.page_organizer_button = RoundedButton(
            card,
            text="PDF 페이지 정리 도구",
            command=self.select_page_organizer_files,
            background="#FFF4F6",
            hover_background="#FFE3E8",
            canvas_background=CARD_BACKGROUND,
            width=540,
            height=48,
            radius=16,
            font=("Segoe UI", 10, "bold"),
        )
        self.page_organizer_button.pack(fill="x", pady=(0, 10))
        self.cancel_button = RoundedButton(
            card,
            text="진행 중인 작업 취소",
            command=self.cancel_current_job,
            background="#F2F4F7",
            hover_background="#E4E7EC",
            canvas_background=CARD_BACKGROUND,
            width=540,
            height=42,
            radius=14,
            font=("Segoe UI", 9, "normal"),
        )
        self.cancel_button.pack(fill="x", pady=(0, 10))
        self.cancel_button.config(state="disabled")
        ttk.Label(
            card,
            text="여러 PDF를 선택하거나 이 창으로 드래그할 수 있습니다.",
            style="Hint.TLabel",
        ).pack(anchor="center", pady=(0, 18))

        status_frame = tk.Frame(
            card,
            bg=STATUS_BACKGROUND,
            highlightbackground="#E4E7EC",
            highlightthickness=1,
            padx=16,
            pady=13,
        )
        status_frame.pack(fill="both", expand=True)
        self.status_label = tk.Label(
            status_frame,
            text="PDF 파일을 선택하면 진행 상황이 여기에 표시됩니다.",
            bg=STATUS_BACKGROUND,
            fg=TEXT_SECONDARY,
            font=("Segoe UI", 10),
            justify="left",
            anchor="nw",
            wraplength=540,
        )
        self.status_label.pack(fill="both", expand=True)
        status_frame.bind("<Configure>", self._resize_status_wrap)

        ttk.Label(
            outer,
            text=COPYRIGHT_TEXT,
            style="Subtitle.TLabel",
        ).pack(side="bottom", pady=(14, 0))

    def _resize_status_wrap(self, event: Any) -> None:
        """Keep long paths readable as the status card changes width."""

        self.status_label.config(wraplength=max(240, int(event.width) - 32))

    def _configure_drag_and_drop(self) -> None:
        if self.dnd_module is None:
            return
        self.root.drop_target_register(self.dnd_module.DND_FILES)
        self.root.dnd_bind("<<Drop>>", self.drop_handler)

    def select_file(self) -> None:
        """Prompt for one or more PDFs and start one sequential batch."""

        selected_paths = filedialog.askopenfilenames(
            parent=self.root,
            filetypes=[("PDF files", "*.pdf")],
        )
        if selected_paths:
            self.start_batch_compression(tuple(Path(path) for path in selected_paths))

    def select_unlock_files(self) -> None:
        """Select one or more PDFs and run the verified unlock workflow."""

        selected_paths = filedialog.askopenfilenames(
            parent=self.root,
            title="보안 해제할 PDF 선택",
            filetypes=[("PDF files", "*.pdf")],
        )
        if selected_paths:
            self.unlock_batch_and_report(tuple(Path(path) for path in selected_paths))

    def select_ocr_files(self) -> None:
        """Select PDFs or images and start cancellable offline OCR."""

        selected_paths = filedialog.askopenfilenames(
            parent=self.root,
            title="OCR 분석할 PDF 또는 이미지 선택",
            filetypes=[
                ("PDF and images", "*.pdf *.jpg *.jpeg *.png *.bmp *.tif *.tiff"),
                ("PDF files", "*.pdf"),
                ("Image files", "*.jpg *.jpeg *.png *.bmp *.tif *.tiff"),
            ],
        )
        if selected_paths:
            self.start_ocr_batch(tuple(Path(path) for path in selected_paths))

    def select_finder_files(self) -> None:
        """Open Finder with directly selected PDF files."""

        selected_paths = filedialog.askopenfilenames(
            parent=self.root,
            title="PDF FINDER 검색 대상 선택",
            filetypes=[("PDF files", "*.pdf")],
        )
        if selected_paths:
            self.open_finder(tuple(Path(path) for path in selected_paths))

    def select_engineering_pdf(self) -> None:
        """Select one drawing PDF and a new structured-result directory."""

        selected = filedialog.askopenfilename(
            parent=self.root,
            title="Engineering 분석할 PDF 선택",
            filetypes=[("PDF files", "*.pdf")],
        )
        if not selected:
            return
        parent = filedialog.askdirectory(
            parent=self.root,
            title="Engineering 분석 결과를 저장할 상위 폴더 선택",
        )
        if not parent:
            return
        source = Path(selected)
        output = _available_engineering_output_directory(Path(parent), source.stem)
        from slimpdf.ui.engineering_setup_window import EngineeringSetupWindow

        EngineeringSetupWindow(
            self.root,
            on_start=lambda selections: self.start_engineering_analysis(
                source, output, selections
            ),
        )

    def open_finder(self, file_paths: Sequence[Path]) -> None:
        """Open the dedicated Finder screen with pre-registered files."""

        from slimpdf.ui.finder_window import FinderWindow

        FinderWindow(self.root, tuple(file_paths))

    def open_news(self) -> None:
        """Open the read-only Daily Shipbuilding News search screen."""

        if not is_news_enabled():
            raise RuntimeError("조선해양 NEWS는 사내 배포판에서만 제공됩니다.")

        from slimpdf.ui.news_window import NewsWindow

        NewsWindow(self.root)

    def select_checker_files(self) -> None:
        """Require exactly two PDFs before opening the Checker setup screen."""

        selected_paths = filedialog.askopenfilenames(
            parent=self.root,
            title="비교할 이전/최신 Revision PDF 2개 선택",
            filetypes=[("PDF files", "*.pdf")],
        )
        if selected_paths:
            self.open_checker(tuple(Path(path) for path in selected_paths))

    def select_page_organizer_files(self) -> None:
        """Select PDFs and ask which page-orientation cleanup mode to use."""

        selected_paths = filedialog.askopenfilenames(
            parent=self.root,
            title="페이지를 정리할 PDF 선택",
            filetypes=[("PDF files", "*.pdf")],
        )
        if not selected_paths:
            return
        answer = messagebox.askyesnocancel(
            "PDF 페이지 정리 도구",
            "정리 방식을 선택하세요.\n\n"
            "[예] 텍스트 방향에 맞춰 페이지 자동 회전\n"
            "[아니요] 모든 가로형 페이지를 세로 방향으로 정렬\n"
            "[취소] 작업 취소",
            parent=self.root,
        )
        if answer is None:
            return
        mode = (
            PageOrganizationMode.AUTO_TEXT
            if answer
            else PageOrganizationMode.PORTRAIT
        )
        self.start_page_organization_batch(
            tuple(Path(path) for path in selected_paths), mode
        )

    def open_checker(self, file_paths: Sequence[Path]) -> bool:
        """Open Checker only for an exact two-file selection."""

        paths = tuple(file_paths)
        if len(paths) != 2:
            messagebox.showwarning(
                "PDF CHECKER",
                f"PDF CHECKER는 정확히 PDF 2개를 선택해야 합니다.\n현재 선택: {len(paths)}개",
                parent=self.root,
            )
            return False
        from slimpdf.ui.checker_window import CheckerWindow

        CheckerWindow(self.root, (paths[0], paths[1]))
        return True

    def cancel_current_job(self) -> None:
        """Request cancellation at the next safe file or page boundary."""

        if self.cancellation_token is not None:
            self.cancellation_token.cancel()
            self.status_label.config(text="작업 취소 요청됨... 안전하게 정리하는 중입니다.")
            self.cancel_button.config(state="disabled")

    def drop_handler(self, event: Any) -> None:
        """Handle one PDF path supplied by tkinterdnd2."""

        dropped_paths = parse_drop_paths(self.root, str(event.data))
        invalid_paths = [path for path in dropped_paths if path.suffix.lower() != ".pdf"]
        if invalid_paths:
            messagebox.showwarning("경고", "PDF 파일만 지원됩니다.", parent=self.root)
            return
        if dropped_paths:
            self.start_batch_compression(dropped_paths)

    def _handle_close_request(self) -> None:
        """Keep the window alive while a non-cancellable publish operation is active."""

        if self.is_compressing:
            messagebox.showwarning(
                "작업 진행 중",
                "PDF 압축과 결과 검증이 진행 중입니다. 작업이 끝난 뒤 창을 닫아주세요.",
                parent=self.root,
            )
            return
        self.root.destroy()

    def _set_busy(self, busy: bool) -> None:
        self.is_compressing = busy
        state = "disabled" if busy else "normal"
        self.file_button.config(state=state)
        self.unlock_button.config(state=state)
        self.ocr_button.config(state=state)
        if hasattr(self, "engineering_button"):
            self.engineering_button.config(state=state)
        self.finder_button.config(state=state)
        news_button = getattr(self, "news_button", None)
        if news_button is not None:
            news_button.config(state=state)
        self.checker_button.config(state=state)
        if hasattr(self, "page_organizer_button"):
            self.page_organizer_button.config(state=state)
        self.cancel_button.config(state="normal" if busy else "disabled")

    def start_engineering_analysis(
        self,
        input_path: Path,
        output_directory: Path,
        reference_selections: Sequence[EngineeringReferenceSelection] = (),
    ) -> bool:
        """Run read-only engineering analysis outside the Tk event loop."""

        if self.is_compressing:
            messagebox.showwarning(
                "작업 진행 중", "현재 작업이 끝난 뒤 다시 시도하세요.", parent=self.root
            )
            return False
        token = CancellationToken()
        self.cancellation_token = token
        self._set_busy(True)
        self.status_label.config(text=f"Engineering 분석 준비 중... {input_path.name}")

        def worker() -> None:
            try:
                selections = tuple(reference_selections)
                if selections:
                    self.root.after(
                        0,
                        self.status_label.config,
                        {"text": f"외부 참조 목록 검증 중... ({len(selections)}개)"},
                    )
                token.raise_if_cancelled()
                reference_records = load_reference_selections(selections)
                token.raise_if_cancelled()
                result = analyze_engineering_pdf(
                    input_path,
                    output_directory,
                    reference_records=reference_records,
                    cancellation=token,
                    on_progress=lambda completed, total, stage: self.root.after(
                        0,
                        self.status_label.config,
                        {
                            "text": (
                                f"Engineering 분석 중... ({completed}/{total}) "
                                f"{stage}"
                            )
                        },
                    ),
                )
            except Exception as error:
                diagnostic_id = uuid4().hex[:8].upper()
                LOGGER.exception(
                    "Engineering analysis failed: diagnostic_id=%s input=%s",
                    diagnostic_id,
                    input_path,
                )
                self.root.after(
                    0, self._finish_engineering_analysis, None, error, diagnostic_id
                )
                return
            self.root.after(0, self._finish_engineering_analysis, result, None, "")

        Thread(target=worker, name="SlimPDF-Engineering-Analysis", daemon=False).start()
        return True

    def _finish_engineering_analysis(self, result, error, diagnostic_id: str) -> None:
        self.cancellation_token = None
        self._set_busy(False)
        if error is not None:
            from slimpdf.core.jobs import JobCancelledError

            if isinstance(error, JobCancelledError):
                self.status_label.config(text="Engineering 분석을 안전하게 취소했습니다.")
                show_readable_message(
                    "warning",
                    "Engineering 분석",
                    "사용자 요청으로 분석을 취소했습니다. 결과는 게시되지 않았습니다.",
                    parent=self.root,
                )
                return
            self.status_label.config(text="Engineering 분석 실패")
            show_readable_message(
                "error",
                "Engineering 분석 오류",
                f"진단 ID: {diagnostic_id}\n{_exception_details(error)}\n\n"
                f"전체 오류 기록: {get_log_file(get_app_data_directory())}",
                parent=self.root,
            )
            return
        self.status_label.config(
            text=(
                f"Engineering 분석 완료: {result.page_count}페이지, "
                f"검토 {result.review_count}건"
            )
        )
        open_result = messagebox.askyesno(
            "Engineering 분석 완료",
            f"페이지: {result.page_count}\n"
            f"텍스트 증거: {result.text_count}\n"
            f"검토 필요: {result.review_count}\n\n"
            f"결과 폴더:\n{result.output_directory}\n\n"
            "검토 화면을 여시겠습니까?",
            parent=self.root,
        )
        if open_result:
            try:
                from slimpdf.ui.engineering_review_window import EngineeringReviewWindow

                EngineeringReviewWindow(
                    self.root, result.input_path, result.output_directory
                )
            except Exception as open_error:
                LOGGER.exception("Engineering review window could not be opened")
                show_readable_message(
                    "error",
                    "Engineering 검토 화면 오류",
                    _exception_details(open_error),
                    parent=self.root,
                )

    def start_page_organization_batch(
        self,
        file_paths: Sequence[Path],
        mode: PageOrganizationMode,
    ) -> bool:
        """Organize selected PDFs sequentially and report exactly once."""

        if self.is_compressing:
            messagebox.showwarning(
                "작업 진행 중", "현재 작업이 끝난 뒤 다시 시도하세요.", parent=self.root
            )
            return False
        paths = tuple(file_paths)
        if not paths:
            return False
        self._set_busy(True)
        self.status_label.config(text=f"페이지 정리 준비 중... (총 {len(paths)}개)")

        def worker() -> None:
            results = []
            failures: list[tuple[Path, Exception, str]] = []
            for index, path in enumerate(paths, start=1):
                self.root.after(
                    0,
                    self.status_label.config,
                    {"text": f"페이지 정리 중... ({index}/{len(paths)}) {path.name}"},
                )
                try:
                    results.append(
                        organize_pdf_pages(PageOrganizationRequest(path, mode))
                    )
                except Exception as error:
                    diagnostic_id = uuid4().hex[:8].upper()
                    LOGGER.exception(
                        "Page organization failed: diagnostic_id=%s mode=%s input=%s",
                        diagnostic_id,
                        mode.value,
                        path,
                    )
                    failures.append((path, error, diagnostic_id))
            self.root.after(
                0,
                self._finish_page_organization_batch,
                results,
                failures,
                len(paths),
            )

        Thread(target=worker, name="SlimPDF-PageOrganizer", daemon=False).start()
        return True

    def _finish_page_organization_batch(self, results, failures, total: int) -> None:
        """Restore controls and display one complete page-organization summary."""

        self._set_busy(False)
        self.status_label.config(
            text=f"페이지 정리 완료: 성공 {len(results)}개 / 실패 {len(failures)}개"
        )
        lines = [f"전체 {total}개", f"성공 {len(results)}개", f"실패 {len(failures)}개"]
        if results:
            lines.append("\n성공 결과:")
            for result in results:
                lines.append(
                    f"• {result.input_path.name} → {result.output_path.name} "
                    f"(회전 {len(result.rotated_pages)}페이지)"
                )
                lines.extend(f"  주의: {warning}" for warning in result.warnings)
        if failures:
            lines.append("\n실패 결과:")
            lines.extend(
                f"• {path.name} [{diagnostic_id}]\n  {_exception_details(error)}"
                for path, error, diagnostic_id in failures
            )
            lines.append(f"\n전체 오류 기록: {get_log_file(get_app_data_directory())}")
        message = "\n".join(lines)
        if failures or any(result.warnings for result in results):
            show_readable_message("warning", "SlimPDF 페이지 정리 결과", message, parent=self.root)
        else:
            show_readable_message("info", "SlimPDF 페이지 정리 결과", message, parent=self.root)

    def _confirm_signature_invalidation(self, file_paths: Sequence[Path]) -> bool | None:
        """Silently authorize signed-copy compression for detected signed PDFs.

        The original is never modified and the final batch result still reports
        that electronic signature properties were removed from the new copy.
        """

        signed_paths = tuple(
            path for path in file_paths if path.is_file() and pdf_has_signatures(path)
        )
        if not signed_paths:
            return False
        LOGGER.info(
            "Signed PDF compression auto-authorized for %d file(s); originals remain unchanged",
            len(signed_paths),
        )
        return True

    def start_compression(self, file_path: Path) -> bool:
        """Start a one-file batch for backward-compatible callers."""

        return self.start_batch_compression((file_path,))

    def start_batch_compression(self, file_paths: Sequence[Path]) -> bool:
        """Compress every selected PDF sequentially and report only once."""

        if self.is_compressing:
            messagebox.showwarning("작업 진행 중", "현재 압축 작업이 끝난 뒤 다시 시도하세요.", parent=self.root)
            return False
        paths = tuple(file_paths)
        if not paths:
            return False
        allow_signature_invalidation = self._confirm_signature_invalidation(paths)
        if allow_signature_invalidation is None:
            self.status_label.config(text="작업 취소")
            return False
        self._set_busy(True)
        self.status_label.config(text=f"압축 준비 중... (총 {len(paths)}개)")

        def worker() -> None:
            results: list[CompressionResult] = []
            failures: list[tuple[Path, Exception, str]] = []
            for index, file_path in enumerate(paths, start=1):
                self.root.after(
                    0,
                    self.status_label.config,
                    {"text": f"압축 중... ({index}/{len(paths)}) {file_path.name}"},
                )
                try:
                    result = compress_pdf(
                        CompressionRequest(
                            file_path,
                            self.compression_mode,
                            allow_signature_invalidation=allow_signature_invalidation,
                        )
                    )
                except Exception as error:
                    diagnostic_id = uuid4().hex[:8].upper()
                    LOGGER.exception(
                        "Compression failed: diagnostic_id=%s mode=%s input=%s",
                        diagnostic_id,
                        self.compression_mode.value,
                        file_path,
                    )
                    failures.append((file_path, error, diagnostic_id))
                else:
                    results.append(result)
            self.root.after(0, self._finish_background_batch, results, failures, len(paths))

        Thread(target=worker, name="SlimPDF-Compression", daemon=False).start()
        return True

    def _finish_background_batch(
        self,
        results: Sequence[CompressionResult],
        failures: Sequence[tuple[Path, Exception, str]],
        total: int,
    ) -> None:
        self._set_busy(False)
        self._show_batch_summary(results, failures, total)

    def _finish_background_error(
        self,
        error: Exception,
        file_path: Path,
        mode: CompressionMode,
        diagnostic_id: str,
    ) -> None:
        self._set_busy(False)
        self.status_label.config(text="오류 발생")
        messagebox.showerror(
            "SlimPDF 압축 오류",
            build_compression_error_message(error, file_path, mode, diagnostic_id),
            parent=self.root,
        )

    def _finish_background_success(self, result) -> None:
        self._set_busy(False)
        self.status_label.config(text="압축 완료")
        info = f"압축된 PDF가 저장되었습니다:\n{result.output_path}\n\n압축률: {result.compression_ratio}%"
        if result.diagnostics.warnings:
            info += "\n\n주의:\n" + "\n".join(result.diagnostics.warnings)
            messagebox.showwarning(APP_NAME, info, parent=self.root)
        else:
            messagebox.showinfo(APP_NAME, info, parent=self.root)

    def compress_and_report(self, file_path: Path) -> bool:
        """Compress one PDF through the shared batch reporting path."""

        return self.compress_batch_and_report((file_path,))

    def compress_batch_and_report(self, file_paths: Sequence[Path]) -> bool:
        """Compress files sequentially and display one final summary dialog."""

        paths = tuple(file_paths)
        allow_signature_invalidation = self._confirm_signature_invalidation(paths)
        if allow_signature_invalidation is None:
            self.status_label.config(text="작업 취소")
            return False
        results: list[CompressionResult] = []
        failures: list[tuple[Path, Exception, str]] = []
        for index, file_path in enumerate(paths, start=1):
            self.status_label.config(text=f"압축 중... ({index}/{len(paths)}) {file_path.name}")
            self.root.update_idletasks()
            try:
                results.append(
                    compress_pdf(
                        CompressionRequest(
                            file_path,
                            self.compression_mode,
                            allow_signature_invalidation=allow_signature_invalidation,
                        )
                    )
                )
            except Exception as error:
                diagnostic_id = uuid4().hex[:8].upper()
                LOGGER.exception(
                    "Compression failed: diagnostic_id=%s mode=%s input=%s",
                    diagnostic_id,
                    self.compression_mode.value,
                    file_path,
                )
                failures.append((file_path, error, diagnostic_id))
        self._show_batch_summary(results, failures, len(paths))
        return not failures

    def unlock_batch_and_report(self, file_paths: Sequence[Path]) -> bool:
        """Unlock selected PDFs sequentially and show exactly one final result dialog."""

        paths = tuple(file_paths)
        common_credentials = request_unlock_credentials(self.root)
        if common_credentials is None:
            self.status_label.config(text="작업 취소")
            return False
        allow_signature_invalidation = messagebox.askyesno(
            "전자서명 무효화 경고",
            "PDF 보안 해제는 기존 전자서명을 무효화할 수 있습니다.\n"
            "원본은 변경하지 않고 별도 사본으로 저장합니다.\n\n"
            "위 위험을 이해하고 계속하시겠습니까?",
            parent=self.root,
        )
        if not allow_signature_invalidation:
            self.status_label.config(text="작업 취소")
            return False

        results: list[UnlockResult] = []
        failures: list[tuple[Path, Exception, str]] = []
        for index, file_path in enumerate(paths, start=1):
            self.status_label.config(
                text=f"보안 해제 중... ({index}/{len(paths)}) {file_path.name}"
            )
            self.root.update_idletasks()
            try:
                result = unlock_pdf(
                    UnlockRequest(
                        file_path,
                        common_credentials,
                        allow_signature_invalidation=allow_signature_invalidation,
                    )
                )
            except PdfPasswordRequiredError:
                individual_password = simpledialog.askstring(
                    "개별 PDF 암호",
                    f"공통 암호로 열 수 없습니다.\n\n{file_path.name}\n\n"
                    "개별 암호를 입력하거나 취소하여 이 파일을 건너뛰세요.",
                    show="*",
                    parent=self.root,
                )
                if individual_password is None:
                    error = PdfPasswordRequiredError("사용자가 개별 암호 입력을 건너뛰었습니다.")
                    diagnostic_id = uuid4().hex[:8].upper()
                    failures.append((file_path, error, diagnostic_id))
                    continue
                try:
                    result = unlock_pdf(
                        UnlockRequest(
                            file_path,
                            (PdfCredential("user-entered-individual", individual_password),),
                            allow_signature_invalidation=allow_signature_invalidation,
                        )
                    )
                except Exception as error:
                    diagnostic_id = uuid4().hex[:8].upper()
                    LOGGER.exception(
                        "PDF unlock failed after individual credential: diagnostic_id=%s input=%s",
                        diagnostic_id,
                        file_path,
                    )
                    failures.append((file_path, error, diagnostic_id))
                    continue
            except Exception as error:
                diagnostic_id = uuid4().hex[:8].upper()
                LOGGER.exception(
                    "PDF unlock failed: diagnostic_id=%s input=%s",
                    diagnostic_id,
                    file_path,
                )
                failures.append((file_path, error, diagnostic_id))
                continue
            results.append(result)

        self._show_unlock_batch_summary(results, failures, len(paths))
        return not failures

    def _show_unlock_batch_summary(
        self,
        results: Sequence[UnlockResult],
        failures: Sequence[tuple[Path, Exception, str]],
        total: int,
    ) -> None:
        """Display one summary after an entire PDF unlock batch."""

        self.status_label.config(
            text=f"보안 해제 완료: 성공 {len(results)}개 / 실패·건너뜀 {len(failures)}개"
        )
        lines = [
            f"전체 {total}개",
            f"성공 {len(results)}개",
            f"실패·건너뜀 {len(failures)}개",
        ]
        if results:
            lines.append("\n성공 결과:")
            lines.extend(
                f"• {result.input_path.name} → {result.output_path.name}"
                for result in results
            )
        if failures:
            lines.append("\n실패·건너뜀 결과:")
            lines.extend(
                f"• {path.name} [{diagnostic_id}]\n  {_exception_details(error)}"
                for path, error, diagnostic_id in failures
            )
            lines.append(f"\n전체 오류 기록: {get_log_file(get_app_data_directory())}")
        message = "\n".join(lines)
        if failures:
            show_readable_message("warning", "SlimPDF PDF 보안 해제 결과", message, parent=self.root)
        else:
            show_readable_message("info", "SlimPDF PDF 보안 해제 결과", message, parent=self.root)

    def _confirm_ocr_signature_invalidation(self, paths: Sequence[Path]) -> bool | None:
        """Require one explicit warning before OCR modifies any signed PDF copy."""

        signed_paths = []
        for path in paths:
            if path.suffix.lower() != ".pdf" or not path.is_file():
                continue
            try:
                if pdf_has_signatures(path):
                    signed_paths.append(path)
            except Exception:
                continue
        if not signed_paths:
            return False
        preview = "\n".join(f"• {path.name}" for path in signed_paths[:5])
        confirmed = messagebox.askyesno(
            "전자서명 무효화 경고",
            "OCR 텍스트 계층을 추가하면 기존 전자서명이 무효화될 수 있습니다.\n"
            "원본은 변경하지 않고 별도 _ocr.pdf 사본을 생성합니다.\n\n"
            f"대상 파일:\n{preview}\n\n계속하시겠습니까?",
            parent=self.root,
        )
        return True if confirmed else None

    def start_ocr_batch(self, file_paths: Sequence[Path]) -> bool:
        """Start an offline OCR batch without blocking the Tk event loop."""

        if self.is_compressing:
            messagebox.showwarning(
                "작업 진행 중", "현재 작업이 끝난 뒤 다시 시도하세요.", parent=self.root
            )
            return False
        paths = tuple(file_paths)
        allow_signature_invalidation = self._confirm_ocr_signature_invalidation(paths)
        if allow_signature_invalidation is None:
            return False
        token = CancellationToken()
        self.cancellation_token = token
        self._set_busy(True)
        self.status_label.config(text=f"OCR 준비 중... (총 {len(paths)}개)")

        def worker() -> None:
            batch = run_sequential_job(
                paths,
                lambda path, current_token: analyze_ocr(
                    OcrRequest(
                        path,
                        allow_signature_invalidation=allow_signature_invalidation,
                    ),
                    cancellation=current_token,
                ),
                cancellation=token,
                on_progress=lambda progress: self.root.after(
                    0,
                    self.status_label.config,
                    {
                        "text": (
                            f"OCR 분석 중... ({progress.completed + 1}/{progress.total}) "
                            f"{progress.current_path.name}"
                        )
                    },
                ),
            )
            self.root.after(0, self._finish_ocr_batch, batch)

        Thread(target=worker, name="SlimPDF-OCR", daemon=False).start()
        return True

    def ocr_batch_and_report(self, file_paths: Sequence[Path]) -> bool:
        """Run Explorer-triggered OCR sequentially and display one result dialog."""

        paths = tuple(file_paths)
        allow_signature_invalidation = self._confirm_ocr_signature_invalidation(paths)
        if allow_signature_invalidation is None:
            return False
        token = CancellationToken()
        self.cancellation_token = token
        batch = run_sequential_job(
            paths,
            lambda path, current_token: analyze_ocr(
                OcrRequest(
                    path,
                    allow_signature_invalidation=allow_signature_invalidation,
                ),
                cancellation=current_token,
            ),
            cancellation=token,
            on_progress=lambda progress: (
                self.status_label.config(
                    text=(
                        f"OCR 분석 중... ({progress.completed + 1}/{progress.total}) "
                        f"{progress.current_path.name}"
                    )
                ),
                self.root.update_idletasks(),
            ),
        )
        self.cancellation_token = None
        self._show_ocr_batch_summary(batch)
        return not batch.failed and not batch.cancelled

    def _finish_ocr_batch(self, batch) -> None:
        self.cancellation_token = None
        self._set_busy(False)
        self._show_ocr_batch_summary(batch)

    def _show_ocr_batch_summary(self, batch) -> None:
        """Show OCR success, low-confidence warnings, failures, and cancellation once."""

        successes = batch.succeeded
        failures = batch.failed
        skipped = batch.skipped
        self.status_label.config(
            text=(
                f"OCR 완료: 성공 {len(successes)}개 / 실패 {len(failures)}개 / "
                f"건너뜀 {len(skipped)}개"
            )
        )
        lines = [
            f"전체 {len(batch.items)}개",
            f"성공 {len(successes)}개",
            f"실패 {len(failures)}개",
            f"건너뜀 {len(skipped)}개",
        ]
        low_confidence = []
        if successes:
            lines.append("\n생성 결과:")
            for item in successes:
                result: OcrResult = item.value
                lines.append(
                    f"• {result.input_path.name} → {result.output_pdf_path.name}, "
                    f"{result.output_text_path.name}"
                )
                if result.low_confidence_pages:
                    low_confidence.append(
                        f"{result.input_path.name}: {', '.join(map(str, result.low_confidence_pages))}페이지"
                    )
        if low_confidence:
            lines.append("\n낮은 OCR 신뢰도:")
            lines.extend(f"• {value}" for value in low_confidence)
        if failures:
            lines.append("\n실패 결과:")
            for item in failures:
                diagnostic_id = uuid4().hex[:8].upper()
                LOGGER.error(
                    "OCR failed: diagnostic_id=%s input=%s error=%s",
                    diagnostic_id,
                    item.input_path,
                    item.error,
                )
                lines.append(
                    f"• {item.input_path.name} [{diagnostic_id}]\n  {_exception_details(item.error)}"
                )
        if batch.cancelled:
            lines.append("\n사용자 요청으로 안전하게 취소했습니다.")
        message = "\n".join(lines)
        if failures:
            show_readable_message("error", "SlimPDF OCR 결과", message, parent=self.root)
        elif low_confidence or batch.cancelled:
            show_readable_message("warning", "SlimPDF OCR 결과", message, parent=self.root)
        else:
            show_readable_message("info", "SlimPDF OCR 결과", message, parent=self.root)

    def _show_batch_summary(
        self,
        results: Sequence[CompressionResult],
        failures: Sequence[tuple[Path, Exception, str]],
        total: int,
    ) -> None:
        """Display exactly one result dialog for an entire compression batch."""

        self.status_label.config(text=f"작업 완료: 성공 {len(results)}개 / 실패 {len(failures)}개")
        lines = [f"전체 {total}개", f"성공 {len(results)}개", f"실패 {len(failures)}개"]
        if results:
            lines.append("\n성공 결과:")
            lines.extend(
                f"• {result.input_path.name} → {result.output_path.name} ({result.compression_ratio}%)"
                for result in results
            )
            result_warnings = tuple(
                dict.fromkeys(
                    warning
                    for result in results
                    for warning in result.diagnostics.warnings
                )
            )
            if result_warnings:
                lines.append("\n주의:")
                lines.extend(f"• {warning}" for warning in result_warnings)
        if failures:
            lines.append("\n실패 결과:")
            lines.extend(
                f"• {path.name} [{diagnostic_id}]\n  {_exception_details(error)}"
                for path, error, diagnostic_id in failures
            )
            lines.append(f"\n전체 오류 기록: {get_log_file(get_app_data_directory())}")
        message = "\n".join(lines)
        if failures:
            show_readable_message("error", "SlimPDF 일괄 압축 결과", message, parent=self.root, topmost=True)
        elif any(result.diagnostics.warnings for result in results):
            show_readable_message("warning", "SlimPDF 일괄 압축 결과", message, parent=self.root, topmost=True)
        else:
            show_readable_message("info", "SlimPDF 일괄 압축 결과", message, parent=self.root, topmost=True)
