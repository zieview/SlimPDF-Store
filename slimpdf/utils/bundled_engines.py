"""Locate engines shipped beside a frozen application's resources."""
import sys
from pathlib import Path


def find_bundled_engine(relative_path: str) -> Path | None:
    """Avoid consulting installed engines when a packaged engine is available."""
    if not getattr(sys, "frozen", False):
        return None
    roots = [Path(sys.executable).resolve().parent]
    if hasattr(sys, "_MEIPASS"):
        roots.insert(0, Path(sys._MEIPASS))
    for root in roots:
        candidate = root / relative_path
        if candidate.is_file():
            return candidate
    return None
