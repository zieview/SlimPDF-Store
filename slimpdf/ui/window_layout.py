"""Responsive window sizing and readable long-message dialogs."""

from __future__ import annotations

import tkinter as tk
import sys
from tkinter import messagebox, ttk
from typing import Literal

from slimpdf.ui.theme import apply_modern_theme


WINDOW_MARGIN = 72
LONG_MESSAGE_LENGTH = 700
LONG_MESSAGE_LINES = 14


def _screen_work_area(window: tk.Misc) -> tuple[int, int, int, int]:
    """Return the Windows work area, with a portable full-screen fallback."""

    screen_width = max(320, int(window.winfo_screenwidth()))
    screen_height = max(240, int(window.winfo_screenheight()))
    if sys.platform == "win32" and isinstance(window, tk.Misc):
        try:
            import ctypes

            class Rect(ctypes.Structure):
                _fields_ = [
                    ("left", ctypes.c_long),
                    ("top", ctypes.c_long),
                    ("right", ctypes.c_long),
                    ("bottom", ctypes.c_long),
                ]

            rectangle = Rect()
            if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rectangle), 0
            ):
                return (
                    rectangle.left,
                    rectangle.top,
                    rectangle.right,
                    rectangle.bottom,
                )
        except (AttributeError, OSError, TypeError):
            pass
    return 0, 0, screen_width, screen_height


def fit_window_to_screen(
    window: tk.Misc,
    width: int,
    height: int,
    *,
    minimum_width: int = 640,
    minimum_height: int = 480,
) -> tuple[int, int]:
    """Fit and centre a window inside the usable screen-sized safety margin."""

    left, top, right, bottom = _screen_work_area(window)
    work_width = max(320, right - left)
    work_height = max(240, bottom - top)
    available_width = max(320, work_width - WINDOW_MARGIN)
    available_height = max(240, work_height - WINDOW_MARGIN)
    actual_width = min(width, available_width)
    actual_height = min(height, available_height)
    safe_min_width = min(minimum_width, actual_width)
    safe_min_height = min(minimum_height, actual_height)
    x = max(left, left + (work_width - actual_width) // 2)
    y = max(top, top + (work_height - actual_height) // 2)
    window.geometry(f"{actual_width}x{actual_height}+{x}+{y}")
    window.minsize(safe_min_width, safe_min_height)
    return actual_width, actual_height


def show_readable_message(
    kind: Literal["info", "warning", "error"],
    title: str,
    message: str,
    *,
    parent: tk.Misc,
    topmost: bool = False,
) -> None:
    """Use a native message box for short text and a scrollable dialog for long text."""

    if len(message) <= LONG_MESSAGE_LENGTH and message.count("\n") < LONG_MESSAGE_LINES:
        if topmost:
            previous = parent.attributes("-topmost")
            try:
                parent.attributes("-topmost", True)
                parent.lift()
                getattr(messagebox, f"show{kind}")(title, message, parent=parent)
            finally:
                parent.attributes("-topmost", previous)
        else:
            getattr(messagebox, f"show{kind}")(title, message, parent=parent)
        return
    _ScrollableMessageDialog(parent, title, message, kind, topmost=topmost)


class _ScrollableMessageDialog:
    """Modal, resizable detail dialog that never grows beyond the current screen."""

    def __init__(
        self, parent: tk.Misc, title: str, message: str, kind: str, *, topmost: bool = False
    ) -> None:
        self.window = tk.Toplevel(parent)
        self.window.title(title)
        self.window.transient(parent)
        if topmost:
            self.window.attributes("-topmost", True)
        apply_modern_theme(self.window)
        fit_window_to_screen(
            self.window, 760, 620, minimum_width=520, minimum_height=360
        )

        outer = ttk.Frame(self.window, padding=18)
        outer.pack(fill="both", expand=True)
        heading = {"error": "오류", "warning": "확인 필요", "info": "작업 결과"}[kind]
        colour = {"error": "#B42318", "warning": "#B54708", "info": "#175CD3"}[kind]
        ttk.Label(
            outer,
            text=heading,
            foreground=colour,
            font=("Segoe UI", 13, "bold"),
        ).pack(anchor="w", pady=(0, 10))

        text_frame = ttk.Frame(outer)
        text_frame.pack(fill="both", expand=True)
        text = tk.Text(
            text_frame,
            wrap="word",
            font=("맑은 고딕", 10),
            background="#FFFFFF",
            foreground="#20242C",
            relief="solid",
            borderwidth=1,
            padx=12,
            pady=10,
        )
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        text.insert("1.0", message)
        text.configure(state="disabled")

        ttk.Button(outer, text="확인", command=self.window.destroy).pack(
            anchor="e", pady=(12, 0)
        )
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        self.window.bind("<Escape>", lambda _event: self.window.destroy())
        self.window.grab_set()
        self.window.lift()
        self.window.focus_set()
        self.window.wait_window()
