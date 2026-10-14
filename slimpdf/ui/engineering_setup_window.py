"""External reference-list setup for read-only engineering analysis."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from slimpdf.engineering_ocr_models import (
    EngineeringReferenceSelection,
    ExternalListKind,
)
from slimpdf.ui.theme import apply_modern_theme
from slimpdf.ui.window_layout import fit_window_to_screen


StartCallback = Callable[[tuple[EngineeringReferenceSelection, ...]], bool]
KIND_LABELS = {
    "BOM": ExternalListKind.BOM,
    "Equipment List": ExternalListKind.EQUIPMENT,
    "Valve List": ExternalListKind.VALVE,
    "Line List": ExternalListKind.LINE,
}


def build_reference_selection(
    path_text: str, kind_label: str, tag_column: str = ""
) -> EngineeringReferenceSelection:
    """Validate setup values without requiring a live Tk window."""

    path = Path(path_text.strip()).expanduser()
    if not path_text.strip():
        raise ValueError("참조 목록 파일을 선택하세요.")
    if path.suffix.casefold() not in {".csv", ".xlsx"}:
        raise ValueError("CSV 또는 XLSX만 지원합니다.")
    try:
        list_kind = KIND_LABELS[kind_label]
    except KeyError as error:
        raise ValueError("지원하지 않는 참조 목록 종류입니다.") from error
    return EngineeringReferenceSelection(
        path=path,
        list_kind=list_kind,
        tag_column=tag_column.strip() or None,
    )


class EngineeringSetupWindow:
    """Collect optional reference-list metadata before analysis starts."""

    def __init__(self, parent: tk.Misc, *, on_start: StartCallback) -> None:
        self.on_start = on_start
        self.selections: list[EngineeringReferenceSelection] = []
        self.window = tk.Toplevel(parent)
        self.window.title("SlimPDF Engineering 분석 설정")
        self.window.configure(background="#FFFFFF")
        self.window.transient(parent)
        fit_window_to_screen(
            self.window, 820, 560, minimum_width=660, minimum_height=440
        )
        apply_modern_theme(self.window)
        self._build()
        self.window.grab_set()
        self.window.focus_set()

    def _build(self) -> None:
        outer = ttk.Frame(self.window, padding=20)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer, text="Engineering 분석 설정", font=("Segoe UI", 17, "bold")
        ).pack(anchor="w")
        ttk.Label(
            outer,
            text=(
                "외부 목록은 선택 사항입니다. TAG 열이 명확하면 비워 두고, "
                "모호한 경우에만 열 이름을 입력하세요."
            ),
            wraplength=760,
        ).pack(anchor="w", pady=(3, 14))

        form = ttk.LabelFrame(outer, text="참조 목록 추가", padding=12)
        form.pack(fill="x")
        self.path_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.path_var, state="readonly").grid(
            row=0, column=0, columnspan=3, sticky="ew"
        )
        ttk.Button(form, text="파일 선택", command=self._choose_file).grid(
            row=0, column=3, padx=(8, 0)
        )
        ttk.Label(form, text="목록 종류").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.kind_var = tk.StringVar(value="Equipment List")
        ttk.Combobox(
            form,
            textvariable=self.kind_var,
            values=tuple(KIND_LABELS),
            state="readonly",
            width=18,
        ).grid(row=1, column=1, sticky="w", padx=(8, 16), pady=(10, 0))
        ttk.Label(form, text="TAG 열 이름 (선택)").grid(
            row=1, column=2, sticky="e", pady=(10, 0)
        )
        self.tag_column_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.tag_column_var, width=20).grid(
            row=1, column=3, sticky="ew", padx=(8, 0), pady=(10, 0)
        )
        ttk.Button(form, text="목록에 추가", command=self._add_selection).grid(
            row=2, column=3, sticky="e", pady=(10, 0)
        )
        form.columnconfigure(0, weight=1)
        form.columnconfigure(2, weight=1)

        list_frame = ttk.LabelFrame(outer, text="선택된 외부 목록", padding=10)
        list_frame.pack(fill="both", expand=True, pady=(12, 0))
        self.tree = ttk.Treeview(
            list_frame,
            columns=("file", "kind", "column"),
            show="headings",
            selectmode="extended",
        )
        for name, label, width in (
            ("file", "파일", 420),
            ("kind", "종류", 130),
            ("column", "TAG 열", 110),
        ):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, anchor="w")
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="선택 제거", command=self._remove_selected).pack(side="left")
        ttk.Button(actions, text="취소", command=self.window.destroy).pack(side="right")
        ttk.Button(actions, text="분석 시작", command=self._start).pack(
            side="right", padx=(0, 8)
        )

    def _choose_file(self) -> None:
        selected = filedialog.askopenfilename(
            parent=self.window,
            title="Engineering 참조 목록 선택",
            filetypes=[("Reference lists", "*.csv *.xlsx"), ("CSV", "*.csv"), ("Excel", "*.xlsx")],
        )
        if selected:
            self.path_var.set(selected)

    def _add_selection(self) -> None:
        try:
            selection = build_reference_selection(
                self.path_var.get(),
                self.kind_var.get(),
                self.tag_column_var.get(),
            )
        except ValueError as error:
            messagebox.showwarning(
                "Engineering 분석 설정", str(error), parent=self.window
            )
            return
        key = (
            selection.path.resolve(),
            selection.list_kind,
            (selection.tag_column or "").casefold(),
        )
        if any(
            (item.path.resolve(), item.list_kind, (item.tag_column or "").casefold()) == key
            for item in self.selections
        ):
            messagebox.showwarning("Engineering 분석 설정", "이미 추가된 참조 목록입니다.", parent=self.window)
            return
        if len(self.selections) >= 20:
            messagebox.showwarning("Engineering 분석 설정", "참조 목록은 최대 20개입니다.", parent=self.window)
            return
        self.selections.append(selection)
        index = len(self.selections) - 1
        self.tree.insert(
            "", "end", iid=str(index),
            values=(
                selection.path.name,
                self.kind_var.get(),
                selection.tag_column or "자동 탐지",
            ),
        )
        self.path_var.set("")
        self.tag_column_var.set("")

    def _remove_selected(self) -> None:
        indexes = {int(value) for value in self.tree.selection()}
        if not indexes:
            return
        self.selections = [
            item for index, item in enumerate(self.selections) if index not in indexes
        ]
        for item in self.tree.get_children():
            self.tree.delete(item)
        for index, selection in enumerate(self.selections):
            label = next(
                name for name, kind in KIND_LABELS.items() if kind is selection.list_kind
            )
            self.tree.insert(
                "", "end", iid=str(index),
                values=(selection.path.name, label, selection.tag_column or "자동 탐지"),
            )

    def _start(self) -> None:
        if self.on_start(tuple(self.selections)):
            self.window.grab_release()
            self.window.destroy()
