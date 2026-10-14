"""Cross-process serialization for Windows Explorer context-menu runs."""

import ctypes
import json
import logging
import os
import sys
import time
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from collections.abc import Iterator
from enum import Enum
from pathlib import Path

from slimpdf.config import (
    CONTEXT_BATCH_DEBOUNCE_SECONDS,
    CONTEXT_BATCH_DIRECTORY_NAME,
    CONTEXT_BATCH_STALE_SECONDS,
    CONTEXT_LOCK_TIMEOUT_MS,
)
from slimpdf.utils.paths import get_app_data_directory

LOGGER = logging.getLogger(__name__)

MUTEX_NAME = "Local\\SlimPDF_ContextCompression"
WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102


class CompressionLockTimeoutError(TimeoutError):
    """Raised when another Explorer compression does not finish in time."""


def _context_queue_directory() -> Path:
    return get_app_data_directory() / CONTEXT_BATCH_DIRECTORY_NAME


def enqueue_context_request(
    input_path: Path,
    mode: Enum,
    *,
    queue_directory: Path | None = None,
) -> Path:
    """Persist one Explorer request so the first process can collect the selection."""

    directory = queue_directory or _context_queue_directory()
    directory.mkdir(parents=True, exist_ok=True)
    request_id = uuid.uuid4().hex
    request_path = directory / f"{request_id}.json"
    temporary_path = directory / f".{request_id}.tmp"
    payload = {
        "input_path": str(input_path.resolve()),
        "mode": mode.name,
        "created_ns": time.time_ns(),
        "pid": os.getpid(),
    }
    temporary_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary_path, request_path)
    LOGGER.info("Context request queued: request=%s input=%s mode=%s", request_id, input_path, mode.value)
    return request_path


def collect_context_batch(
    own_request_path: Path,
    mode: Enum,
    *,
    debounce_seconds: float = CONTEXT_BATCH_DEBOUNCE_SECONDS,
) -> tuple[Path, ...]:
    """Collect same-mode requests after Explorer has launched all selected items."""

    if not own_request_path.exists():
        LOGGER.info("Context request was already collected by another SlimPDF process")
        return ()
    deadline = time.monotonic() + max(0.0, debounce_seconds)
    previous_count = -1
    while time.monotonic() < deadline:
        count = len(list(own_request_path.parent.glob("*.json")))
        if count != previous_count:
            previous_count = count
            deadline = time.monotonic() + max(0.0, debounce_seconds)
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    now_ns = time.time_ns()
    requests: list[tuple[int, Path, Path]] = []
    for request_path in own_request_path.parent.glob("*.json"):
        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            created_ns = int(payload["created_ns"])
            if now_ns - created_ns > CONTEXT_BATCH_STALE_SECONDS * 1_000_000_000:
                request_path.unlink(missing_ok=True)
                LOGGER.warning("Removed stale context request: %s", request_path)
                continue
            if payload["mode"] != mode.name:
                continue
            input_path = Path(payload["input_path"])
            requests.append((created_ns, input_path, request_path))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            LOGGER.warning("Ignoring invalid context request %s: %s", request_path, error)
            request_path.unlink(missing_ok=True)
    requests.sort(key=lambda item: (item[0], str(item[1]).casefold()))
    for _created_ns, _input_path, request_path in requests:
        request_path.unlink(missing_ok=True)
    paths = tuple(input_path for _created_ns, input_path, _request_path in requests)
    LOGGER.info("Context batch collected: mode=%s files=%d", mode.value, len(paths))
    return paths


@contextmanager
def context_compression_lock() -> Iterator[None]:
    """Allow only one Explorer-triggered compression process at a time.

    A named Windows mutex is shared by separately launched SlimPDF processes.
    The lock is intentionally held until the result dialog closes, ensuring
    that one context-menu job fully finishes before the next window starts.
    """

    if sys.platform != "win32":
        LOGGER.warning("Cross-process compression locking is only available on Windows")
        yield
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.ReleaseMutex.argtypes = (wintypes.HANDLE,)
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not mutex_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    acquired = False
    try:
        LOGGER.info("Waiting for another context-menu compression job to finish")
        wait_result = kernel32.WaitForSingleObject(mutex_handle, CONTEXT_LOCK_TIMEOUT_MS)
        if wait_result == WAIT_TIMEOUT:
            raise CompressionLockTimeoutError(
                "다른 SlimPDF 압축 작업이 아직 진행 중입니다. 잠시 후 다시 시도하세요."
            )
        if wait_result not in (WAIT_OBJECT_0, WAIT_ABANDONED):
            raise OSError(f"압축 실행 잠금을 기다리는 중 오류가 발생했습니다: {wait_result}")
        acquired = True
        LOGGER.info("Context-menu compression lock acquired")
        yield
    finally:
        if acquired and not kernel32.ReleaseMutex(mutex_handle):
            LOGGER.error("Failed to release the context-menu compression lock")
        kernel32.CloseHandle(mutex_handle)
        LOGGER.info("Context-menu compression lock released")
