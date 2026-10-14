"""Shared sequential job execution for long-running SlimPDF features."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Event
from typing import Generic, TypeVar


ResultT = TypeVar("ResultT")


class JobCancelledError(RuntimeError):
    """Raised at a safe cancellation boundary between file or page operations."""


class JobItemStatus(str, Enum):
    """Final state of one input item."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class JobProgress:
    """Progress snapshot emitted before each sequential item is processed."""

    completed: int
    total: int
    current_path: Path

    @property
    def percent(self) -> int:
        """Return a stable integer percentage for UI progress bars."""

        return 100 if self.total == 0 else int(self.completed * 100 / self.total)


@dataclass(frozen=True, slots=True)
class JobItemResult(Generic[ResultT]):
    """Success, failure, or skip information for one input path."""

    input_path: Path
    status: JobItemStatus
    value: ResultT | None = None
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class BatchJobResult(Generic[ResultT]):
    """Aggregate result returned once after a sequential batch."""

    items: tuple[JobItemResult[ResultT], ...]
    cancelled: bool = False

    @property
    def succeeded(self) -> tuple[JobItemResult[ResultT], ...]:
        return tuple(item for item in self.items if item.status is JobItemStatus.SUCCEEDED)

    @property
    def failed(self) -> tuple[JobItemResult[ResultT], ...]:
        return tuple(item for item in self.items if item.status is JobItemStatus.FAILED)

    @property
    def skipped(self) -> tuple[JobItemResult[ResultT], ...]:
        return tuple(item for item in self.items if item.status is JobItemStatus.SKIPPED)


class CancellationToken:
    """Thread-safe cooperative cancellation token."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise JobCancelledError("사용자가 작업을 취소했습니다.")


Processor = Callable[[Path, CancellationToken], ResultT]
ProgressCallback = Callable[[JobProgress], None]


def run_sequential_job(
    input_paths: Sequence[Path],
    processor: Processor[ResultT],
    *,
    cancellation: CancellationToken | None = None,
    on_progress: ProgressCallback | None = None,
) -> BatchJobResult[ResultT]:
    """Process all paths in order, preserving failures and stopping safely on cancel."""

    token = cancellation or CancellationToken()
    paths = tuple(input_paths)
    results: list[JobItemResult[ResultT]] = []
    for index, input_path in enumerate(paths):
        if token.is_cancelled:
            results.extend(
                JobItemResult(path, JobItemStatus.SKIPPED)
                for path in paths[index:]
            )
            return BatchJobResult(tuple(results), cancelled=True)
        if on_progress is not None:
            on_progress(JobProgress(index, len(paths), input_path))
        try:
            value = processor(input_path, token)
        except JobCancelledError:
            results.append(JobItemResult(input_path, JobItemStatus.SKIPPED))
            results.extend(
                JobItemResult(path, JobItemStatus.SKIPPED)
                for path in paths[index + 1 :]
            )
            return BatchJobResult(tuple(results), cancelled=True)
        except Exception as error:
            results.append(
                JobItemResult(input_path, JobItemStatus.FAILED, error=error)
            )
        else:
            results.append(
                JobItemResult(input_path, JobItemStatus.SUCCEEDED, value=value)
            )
    return BatchJobResult(tuple(results))

