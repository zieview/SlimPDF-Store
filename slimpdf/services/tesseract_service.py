"""Offline Tesseract discovery and TSV OCR integration."""

from __future__ import annotations

import csv
import os
import shutil
import subprocess
import sys
from functools import lru_cache
from io import StringIO
from pathlib import Path

from slimpdf.ocr_models import OcrTextResult, OcrWord
from slimpdf.utils.bundled_engines import find_bundled_engine


TESSERACT_TIMEOUT_SECONDS = 180


class TesseractNotFoundError(RuntimeError):
    """Raised when the offline OCR executable cannot be found."""


class TesseractLanguageError(RuntimeError):
    """Raised when required OCR language data is unavailable."""


class TesseractExecutionError(RuntimeError):
    """Raised when Tesseract fails or times out."""


def find_tesseract_executable() -> Path:
    """Find Tesseract in PATH or standard Windows installation locations."""

    bundled = find_bundled_engine("engines/tesseract/tesseract.exe")
    if bundled is not None:
        return bundled
    for name in ("tesseract.exe", "tesseract"):
        discovered = shutil.which(name)
        if discovered:
            return Path(discovered)
    candidates = []
    candidates.extend(
        (
            Path(sys.executable).resolve().parent / "Tesseract-OCR" / "tesseract.exe",
            Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
            / "Tesseract-OCR"
            / "tesseract.exe",
        )
    )
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates.append(Path(base) / "Tesseract-OCR" / "tesseract.exe")
    candidates.append(Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise TesseractNotFoundError(
        "오프라인 OCR 엔진 Tesseract를 찾을 수 없습니다. Tesseract와 kor/eng 언어 데이터를 설치하세요."
    )


@lru_cache(maxsize=8)
def list_tesseract_languages(executable: Path | None = None) -> tuple[str, ...]:
    """Return installed language identifiers reported by Tesseract."""

    command = executable or find_tesseract_executable()
    try:
        completed = subprocess.run(
            [str(command), "--list-langs"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TesseractExecutionError(f"Tesseract 언어 목록 확인에 실패했습니다: {error}") from error
    return tuple(
        line.strip()
        for line in completed.stdout.splitlines()
        if line.strip() and not line.startswith("List of available languages")
    )


def recognize_image(
    image_path: Path,
    languages: tuple[str, ...] = ("kor", "eng"),
    *,
    executable: Path | None = None,
) -> OcrTextResult:
    """Recognize one image locally and return word boxes from Tesseract TSV."""

    return recognize_image_with_psm(
        image_path, languages, psm=3, executable=executable
    )


def recognize_image_with_psm(
    image_path: Path,
    languages: tuple[str, ...] = ("kor", "eng"),
    *,
    psm: int,
    executable: Path | None = None,
) -> OcrTextResult:
    """Recognize an image with an explicitly selected Tesseract page layout."""

    if psm not in range(0, 14):
        raise ValueError(f"지원하지 않는 Tesseract PSM입니다: {psm}")

    command = executable or find_tesseract_executable()
    installed = set(list_tesseract_languages(command))
    missing = tuple(language for language in languages if language not in installed)
    if missing:
        raise TesseractLanguageError(
            "Tesseract 언어 데이터가 없습니다: " + ", ".join(missing)
        )
    try:
        completed = subprocess.run(
            [
                str(command),
                str(image_path),
                "stdout",
                "-l",
                "+".join(languages),
                "--psm",
                str(psm),
                "tsv",
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TESSERACT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise TesseractExecutionError("Tesseract OCR 제한 시간을 초과했습니다.") from error
    except (OSError, subprocess.CalledProcessError) as error:
        details = getattr(error, "stderr", "") or str(error)
        raise TesseractExecutionError(f"Tesseract OCR 실행에 실패했습니다: {details}") from error
    return parse_tesseract_tsv(completed.stdout)


def parse_tesseract_tsv(tsv: str) -> OcrTextResult:
    """Parse Tesseract TSV without trusting malformed numeric fields."""

    words: list[OcrWord] = []
    lines: dict[tuple[str, str, str, str], list[str]] = {}
    reader = csv.DictReader(StringIO(tsv), delimiter="\t")
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row.get("conf", "-1"))
            left = int(row.get("left", "0"))
            top = int(row.get("top", "0"))
            width = int(row.get("width", "0"))
            height = int(row.get("height", "0"))
        except (TypeError, ValueError):
            continue
        if confidence < 0 or width <= 0 or height <= 0:
            continue
        words.append(OcrWord(text, confidence, left, top, width, height))
        key = tuple(row.get(name, "") for name in ("block_num", "par_num", "line_num", "page_num"))
        lines.setdefault(key, []).append(text)
    average = sum(word.confidence for word in words) / len(words) if words else 0.0
    return OcrTextResult(
        words=tuple(words),
        text="\n".join(" ".join(parts) for parts in lines.values()),
        average_confidence=average,
    )
