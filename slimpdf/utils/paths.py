"""Path and packaged-resource helpers."""

import os
import sys
from pathlib import Path

from slimpdf.config import MAX_OUTPUT_SUFFIX
from slimpdf.models import CompressionMode


def get_project_root() -> Path:
    """Return the source project root."""

    return Path(__file__).resolve().parents[2]


def get_runtime_root() -> Path:
    """Return the resource root for source and PyInstaller execution."""

    bundled_root = getattr(sys, "_MEIPASS", None)
    return Path(bundled_root) if bundled_root else get_project_root()


def get_resource_path(file_name: str) -> Path:
    """Resolve a bundled resource without requiring the current directory."""

    return get_runtime_root() / file_name


def get_app_data_directory() -> Path:
    """Return a user-writable directory for logs and future settings."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    base_directory = Path(local_app_data) if local_app_data else Path.home() / ".slimpdf"
    return base_directory / "SlimPDF"


def build_output_path(input_path: Path, mode: CompressionMode) -> Path:
    """Build the legacy-compatible output file path for a compression mode."""

    if mode is not CompressionMode.MAXIMUM:
        raise ValueError(f"지원하지 않는 압축 모드입니다: {mode}")
    return input_path.with_name(f"{input_path.stem}{MAX_OUTPUT_SUFFIX}")


def build_available_output_path(input_path: Path, mode: CompressionMode) -> Path:
    """Return an output path that never overwrites an existing user file."""

    preferred = build_output_path(input_path, mode)
    if not preferred.exists():
        return preferred
    counter = 2
    while True:
        candidate = preferred.with_name(f"{preferred.stem}_{counter}{preferred.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1
