"""Windows Credential Manager adapter for user-scoped PDF passwords.

Generic credentials are readable by other processes running as the same Windows
user.  The ``SlimPDF/PdfUnlock`` namespace identifies company-approved usage;
it is not an application isolation or administrator-only security boundary.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from slimpdf.security_models import PdfCredential


CREDENTIAL_PREFIX = "SlimPDF/PdfUnlock/"
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2


class CredentialManagerError(RuntimeError):
    """Raised when Windows Credential Manager cannot complete an operation."""


class CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


PCREDENTIALW = ctypes.POINTER(CREDENTIALW)
PPCREDENTIALW = ctypes.POINTER(PCREDENTIALW)


def credential_target(identifier: str) -> str:
    """Validate an identifier and return its namespaced credential target."""

    normalized = identifier.strip()
    if not normalized or any(character in normalized for character in "\\/:\r\n\t"):
        raise ValueError("자격 증명 식별자는 영문, 숫자, 공백, 하이픈만 사용할 수 있습니다.")
    return CREDENTIAL_PREFIX + normalized


def _advapi32():
    if sys.platform != "win32":
        raise CredentialManagerError("Windows 자격 증명 관리자는 Windows에서만 지원됩니다.")
    return ctypes.WinDLL("Advapi32.dll", use_last_error=True)


def read_pdf_credential(identifier: str) -> PdfCredential:
    """Read one user-vault password without exposing it to logs or configuration."""

    target = credential_target(identifier)
    api = _advapi32()
    api.CredReadW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(PCREDENTIALW),
    )
    api.CredReadW.restype = wintypes.BOOL
    api.CredFree.argtypes = (ctypes.c_void_p,)
    credential_pointer = PCREDENTIALW()
    if not api.CredReadW(target, CRED_TYPE_GENERIC, 0, ctypes.byref(credential_pointer)):
        raise CredentialManagerError(
            f"승인된 자격 증명을 찾을 수 없습니다: {identifier} (Windows 오류 {ctypes.get_last_error()})"
        )
    try:
        credential = credential_pointer.contents
        blob = ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        password = blob.decode("utf-16-le")
        return PdfCredential(identifier, password)
    finally:
        api.CredFree(credential_pointer)


def list_pdf_credential_identifiers() -> tuple[str, ...]:
    """List only SlimPDF credential identifiers without reading password blobs."""

    api = _advapi32()
    api.CredEnumerateW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(PPCREDENTIALW),
    )
    api.CredEnumerateW.restype = wintypes.BOOL
    api.CredFree.argtypes = (ctypes.c_void_p,)
    count = wintypes.DWORD()
    credentials = PPCREDENTIALW()
    if not api.CredEnumerateW(CREDENTIAL_PREFIX + "*", 0, ctypes.byref(count), ctypes.byref(credentials)):
        error_code = ctypes.get_last_error()
        if error_code == 1168:  # ERROR_NOT_FOUND
            return ()
        raise CredentialManagerError(
            f"자격 증명 목록을 읽지 못했습니다. (Windows 오류 {error_code})"
        )
    try:
        identifiers = []
        for index in range(count.value):
            target = credentials[index].contents.TargetName or ""
            if target.startswith(CREDENTIAL_PREFIX):
                identifiers.append(target[len(CREDENTIAL_PREFIX) :])
        return tuple(sorted(set(identifiers), key=str.casefold))
    finally:
        api.CredFree(credentials)


def write_pdf_credential(identifier: str, password: str) -> None:
    """Store one approved password; callers must enforce administrator access."""

    if not password:
        raise ValueError("빈 암호는 자격 증명 관리자에 저장하지 않습니다.")
    target = credential_target(identifier)
    api = _advapi32()
    api.CredWriteW.argtypes = (ctypes.POINTER(CREDENTIALW), wintypes.DWORD)
    api.CredWriteW.restype = wintypes.BOOL
    blob = password.encode("utf-16-le")
    blob_buffer = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    credential = CREDENTIALW(
        Type=CRED_TYPE_GENERIC,
        TargetName=target,
        Comment="SlimPDF approved PDF unlock credential",
        CredentialBlobSize=len(blob),
        CredentialBlob=ctypes.cast(blob_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        Persist=CRED_PERSIST_LOCAL_MACHINE,
        UserName="SlimPDF",
    )
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise CredentialManagerError(
            f"자격 증명을 저장하지 못했습니다. (Windows 오류 {ctypes.get_last_error()})"
        )


def delete_pdf_credential(identifier: str) -> None:
    """Delete an approved credential by identifier."""

    target = credential_target(identifier)
    api = _advapi32()
    api.CredDeleteW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD)
    api.CredDeleteW.restype = wintypes.BOOL
    if not api.CredDeleteW(target, CRED_TYPE_GENERIC, 0):
        raise CredentialManagerError(
            f"자격 증명을 삭제하지 못했습니다. (Windows 오류 {ctypes.get_last_error()})"
        )
