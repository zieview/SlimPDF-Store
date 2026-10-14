"""Permission-aware temporary workspaces for native PDF operations."""

from __future__ import annotations

import logging
import os
import shutil
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from slimpdf.utils.paths import get_app_data_directory


LOGGER = logging.getLogger(__name__)
TEMP_ROOT_ENVIRONMENT = "SLIMPDF_TEMP_ROOT"
KNOWN_PREFIXES = ("slimpdf-signature-", "slimpdf-gs-", "slimpdf-ocr-")
SOURCE_TEMP_ROOT = Path(__file__).parents[2] / "tests" / "runtime_tmp" / "managed_temp"


class TemporaryWorkspaceError(RuntimeError):
    """Raised when a private writable workspace cannot be created."""


def get_temporary_workspace_root() -> Path:
    """Return a deterministic writable root instead of the process-wide TEMP."""

    configured = os.environ.get(TEMP_ROOT_ENVIRONMENT)
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return get_app_data_directory() / "temp"
    return SOURCE_TEMP_ROOT


@contextmanager
def managed_temporary_directory(prefix: str) -> Iterator[Path]:
    """Yield a private directory and never mask the operation's primary error.

    Cleanup failures are logged for later inspection. They are deliberately not
    raised from ``finally`` because doing so would replace a more useful PDF or
    Ghostscript exception that occurred inside the workspace.
    """

    if prefix not in KNOWN_PREFIXES:
        raise ValueError(f"지원하지 않는 임시 작업 접두사입니다: {prefix}")
    root = get_temporary_workspace_root()
    try:
        # Use the OS primitive here. Several compression unit tests mock
        # ``Path.stat`` for PDF sizes; pathlib's exist_ok branch consults that
        # mocked method when this shared root already exists.
        os.makedirs(root, exist_ok=True)
        workspace = _create_private_workspace(root, prefix)
        probe = workspace / ".write-probe"
        probe.write_bytes(b"SlimPDF")
        probe.unlink()
    except Exception as error:
        raise TemporaryWorkspaceError(
            f"SlimPDF 임시 작업 폴더를 만들거나 쓸 수 없습니다: {root}"
        ) from error
    try:
        yield workspace
    finally:
        try:
            shutil.rmtree(workspace)
        except Exception as cleanup_error:
            LOGGER.warning(
                "Temporary workspace cleanup failed; residual retained for inspection: %s (%s)",
                workspace,
                cleanup_error,
            )


def list_temporary_workspace_residuals() -> tuple[Path, ...]:
    """Return only known SlimPDF workspaces left under the managed root."""

    root = get_temporary_workspace_root()
    if not root.is_dir():
        return ()
    try:
        return tuple(
            path
            for path in sorted(root.iterdir(), key=lambda item: item.name.casefold())
            if path.is_dir() and path.name.startswith(KNOWN_PREFIXES)
        )
    except OSError as error:
        LOGGER.warning("Temporary workspace residual scan failed: %s (%s)", root, error)
        return ()


def _create_private_workspace(root: Path, prefix: str) -> Path:
    """Create a unique directory while inheriting the approved root ACL."""

    for _attempt in range(20):
        workspace = root / f"{prefix}{uuid.uuid4().hex}"
        try:
            workspace.mkdir()
            return workspace
        except FileExistsError:
            continue
    raise TemporaryWorkspaceError("고유한 SlimPDF 임시 작업 폴더를 생성하지 못했습니다.")
