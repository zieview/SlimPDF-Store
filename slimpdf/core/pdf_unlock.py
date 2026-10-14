"""Safe use case for publishing an unlocked PDF copy."""

from __future__ import annotations

import getpass
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from slimpdf.core.compression import publish_output_without_overwrite, validate_input_path
from slimpdf.repositories.audit_repository import UnlockAuditRepository, sha256_file
from slimpdf.security_models import UnlockRequest, UnlockResult
from slimpdf.services.pdf_security_service import decrypt_pdf_to_copy
from slimpdf.services.pdf_service import SignedPdfNotSupportedError, pdf_has_signatures
from slimpdf.utils.paths import get_app_data_directory


def build_available_unlock_path(input_path: Path) -> Path:
    """Return a same-directory output name without overwriting user data."""

    preferred = input_path.with_name(f"{input_path.stem}_unlocked.pdf")
    if not preferred.exists():
        return preferred
    counter = 2
    while True:
        candidate = input_path.with_name(f"{input_path.stem}_unlocked_{counter}.pdf")
        if not candidate.exists():
            return candidate
        counter += 1


def unlock_pdf(
    request: UnlockRequest,
    *,
    audit_repository: UnlockAuditRepository | None = None,
) -> UnlockResult:
    """Decrypt, validate, and atomically publish a separate PDF copy."""

    input_path = validate_input_path(request.input_path)
    repository = audit_repository or UnlockAuditRepository(
        get_app_data_directory() / "audit" / "pdf_unlock.sqlite3"
    )
    input_hash = sha256_file(input_path)
    temporary = input_path.parent / f".{input_path.stem}.unlock-{uuid.uuid4().hex}.tmp.pdf"
    credential_identifier = "not-selected"
    output_path: Path | None = None
    try:
        decryption = decrypt_pdf_to_copy(input_path, temporary, request.credentials)
        credential_identifier = decryption.credential_identifier
        if pdf_has_signatures(temporary) and not request.allow_signature_invalidation:
            raise SignedPdfNotSupportedError(
                "전자서명이 포함된 PDF입니다. 서명 무효화 가능성에 명시적으로 동의해야 합니다."
            )
        while True:
            output_path = build_available_unlock_path(input_path)
            try:
                publish_output_without_overwrite(temporary, output_path)
                break
            except FileExistsError:
                continue
        result = UnlockResult(
            input_path=input_path,
            output_path=output_path,
            credential_identifier=credential_identifier,
            page_count=decryption.page_count,
        )
        _write_audit(
            repository,
            input_path,
            input_hash,
            credential_identifier,
            "success",
            output_path,
        )
        return result
    except Exception:
        temporary.unlink(missing_ok=True)
        _write_audit(
            repository,
            input_path,
            input_hash,
            credential_identifier,
            "failure",
            output_path,
        )
        raise


def _write_audit(
    repository: UnlockAuditRepository,
    input_path: Path,
    input_hash: str,
    credential_identifier: str,
    status: str,
    output_path: Path | None,
) -> None:
    repository.append(
        {
            "windows_user": getpass.getuser(),
            "executed_at_utc": datetime.now(timezone.utc).isoformat(),
            "input_name": input_path.name,
            "input_parent_sha256": hashlib.sha256(
                str(input_path.parent).casefold().encode("utf-8")
            ).hexdigest().upper(),
            "input_sha256": input_hash,
            "credential_identifier": credential_identifier,
            "status": status,
            "output_name": output_path.name if output_path else "",
        }
    )
