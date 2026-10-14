"""Application dialog content and helpers."""

import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from slimpdf.security_models import PdfCredential
from slimpdf.services.windows_credential_service import (
    CredentialManagerError,
    list_pdf_credential_identifiers,
    read_pdf_credential,
)
from slimpdf.ui.theme import apply_modern_theme
from slimpdf.edition import AppEdition, get_app_edition


STORE_LICENSE_TEXT = (
    "SlimPDF 라이선스 안내\n"
    "공개 배포 브랜드: 벨록스스튜디오\n"
    "사용자는 처리 권한이 있는 문서에만 이 프로그램을 사용해야 합니다.\n"
    "Store판 소스코드는 GNU AGPL v3 조건으로 제공합니다.\n"
    "PyMuPDF·MuPDF·Ghostscript 등 제3자 구성요소에는 각각의 라이선스가 적용됩니다.\n"
    "원본 문서를 보존하고 압축·OCR·비교 결과를 직접 확인하세요.\n"
    "구매 및 환불에는 Microsoft Store 정책이 적용됩니다."
)
LICENSE_TEXT = (
    STORE_LICENSE_TEXT
    if get_app_edition() is AppEdition.STORE
    else STORE_LICENSE_TEXT
)


class UnlockCredentialDialog(simpledialog.Dialog):
    """Choose an approved identifier or enter one session-only PDF password."""

    def __init__(self, parent: tk.Misc, identifiers: tuple[str, ...]) -> None:
        apply_modern_theme(parent)
        self.identifiers = identifiers
        self.mode = tk.StringVar(value="registered" if identifiers else "direct")
        self.identifier = tk.StringVar(value=identifiers[0] if identifiers else "")
        self.password = tk.StringVar()
        self.result: tuple[PdfCredential, ...] | None = None
        super().__init__(parent, title="PDF 보안 해제 자격 증명")

    def body(self, master: tk.Misc):
        self.configure(background="#FFFFFF")
        master.configure(background="#FFFFFF")
        ttk.Label(
            master,
            text="승인 자격 증명을 선택하거나 이번 작업에만 사용할 암호를 입력하세요.",
            wraplength=480,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        registered = ttk.Radiobutton(
            master,
            text="Windows 자격 증명 관리자",
            variable=self.mode,
            value="registered",
        )
        registered.grid(row=1, column=0, sticky="w", pady=5)
        identifier_box = ttk.Combobox(
            master,
            textvariable=self.identifier,
            values=self.identifiers,
            state="readonly",
            width=36,
        )
        identifier_box.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=5)
        if not self.identifiers:
            registered.state(["disabled"])
        ttk.Radiobutton(
            master,
            text="직접 입력(저장하지 않음)",
            variable=self.mode,
            value="direct",
        ).grid(row=2, column=0, sticky="w", pady=5)
        password_entry = ttk.Entry(
            master, textvariable=self.password, show="*", width=38
        )
        password_entry.grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=5)
        ttk.Label(
            master,
            text="빈 암호로 열리는 권한 제한 PDF는 직접 입력을 선택하고 비워두세요.",
            foreground="#667085",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        master.columnconfigure(1, weight=1)
        return identifier_box if self.identifiers else password_entry

    def validate(self) -> bool:
        if self.mode.get() == "registered" and not self.identifier.get():
            messagebox.showwarning(
                "선택 필요", "자격 증명 식별자를 선택하세요.", parent=self.parent
            )
            return False
        return True

    def apply(self) -> None:
        if self.mode.get() == "registered":
            try:
                self.result = (read_pdf_credential(self.identifier.get()),)
            except CredentialManagerError as error:
                messagebox.showerror(
                    "자격 증명 조회 실패", str(error), parent=self.parent
                )
                self.result = None
        else:
            password = self.password.get()
            self.result = (
                (PdfCredential("user-entered-common", password),) if password else ()
            )


def request_unlock_credentials(parent: tk.Misc) -> tuple[PdfCredential, ...] | None:
    """Display credential choices without exposing stored password values."""

    try:
        identifiers = list_pdf_credential_identifiers()
    except CredentialManagerError:
        identifiers = ()
    return UnlockCredentialDialog(parent, identifiers).result
