"""Shared white, modern Tk/ttk visual theme for SlimPDF windows."""

import tkinter as tk
from tkinter import ttk

from slimpdf.config import HIGHLIGHT_COLOR, TEXT_PRIMARY, TEXT_SECONDARY


WHITE = "#FFFFFF"
BORDER = "#E4E7EC"
SOFT_PINK = "#FFF4F6"
HOVER_PINK = "#FFE3E8"


def apply_modern_theme(window: tk.Misc) -> ttk.Style:
    """Apply the common SlimPDF palette and typography to a Tk window."""

    window.configure(background=WHITE)
    style = ttk.Style(window)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("TFrame", background=WHITE)
    style.configure(
        "TLabel",
        background=WHITE,
        foreground=TEXT_PRIMARY,
        font=("Segoe UI", 10),
    )
    style.configure(
        "TLabelframe",
        background=WHITE,
        bordercolor=BORDER,
        lightcolor=BORDER,
        darkcolor=BORDER,
        relief="solid",
    )
    style.configure(
        "TLabelframe.Label",
        background=WHITE,
        foreground=TEXT_PRIMARY,
        font=("Segoe UI", 10, "bold"),
    )
    style.configure(
        "TButton",
        background=SOFT_PINK,
        foreground=TEXT_PRIMARY,
        bordercolor="#F4A7B4",
        lightcolor=SOFT_PINK,
        darkcolor=SOFT_PINK,
        padding=(12, 8),
        font=("Segoe UI", 10),
        relief="flat",
    )
    style.map(
        "TButton",
        background=[("active", HOVER_PINK), ("pressed", HIGHLIGHT_COLOR)],
        foreground=[("disabled", "#98A2B3")],
    )
    style.configure(
        "TEntry",
        fieldbackground=WHITE,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER,
        padding=6,
    )
    style.configure(
        "TCombobox",
        fieldbackground=WHITE,
        background=WHITE,
        foreground=TEXT_PRIMARY,
        bordercolor=BORDER,
        padding=5,
    )
    style.map("TCombobox", fieldbackground=[("readonly", WHITE)])
    style.configure("TCheckbutton", background=WHITE, foreground=TEXT_PRIMARY)
    style.configure("TRadiobutton", background=WHITE, foreground=TEXT_PRIMARY)
    style.configure(
        "Treeview",
        background=WHITE,
        fieldbackground=WHITE,
        foreground=TEXT_PRIMARY,
        rowheight=28,
        bordercolor=BORDER,
        font=("Segoe UI", 10),
    )
    style.configure(
        "Treeview.Heading",
        background="#F8FAFC",
        foreground=TEXT_SECONDARY,
        font=("Segoe UI", 10, "bold"),
        relief="flat",
    )
    style.map("Treeview", background=[("selected", HIGHLIGHT_COLOR)])
    return style
