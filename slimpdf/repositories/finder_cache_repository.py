"""User-local, non-encrypted page text cache for PDF Finder."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from slimpdf.utils.paths import get_app_data_directory


LOGGER = logging.getLogger(__name__)
CACHE_SCHEMA = 2
CACHE_RETENTION_DAYS = 30
CACHE_MAX_BYTES = 256 * 1024 * 1024


def load_finder_cache(input_path: Path) -> tuple[dict, ...] | None:
    """Return cached page records only when file identity still matches."""

    cache_path = _cache_path(input_path)
    prune_finder_cache()
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if payload.get("schema") != CACHE_SCHEMA:
            return None
        identity = _identity(input_path)
        if payload.get("identity") != identity:
            return None
        pages = payload.get("pages")
        return tuple(pages) if isinstance(pages, list) else None
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None


def save_finder_cache(input_path: Path, pages: tuple[dict, ...]) -> None:
    """Atomically store non-secret page text in the current user's cache."""

    cache_path = _cache_path(input_path)
    temporary = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": CACHE_SCHEMA,
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "identity": _identity(input_path),
            "pages": pages,
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, cache_path)
        prune_finder_cache()
    except OSError:
        LOGGER.warning("PDF Finder cache could not be saved: %s", cache_path)
    finally:
        temporary.unlink(missing_ok=True)


def _identity(input_path: Path) -> dict[str, object]:
    stat = input_path.stat()
    return {
        "path": str(input_path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _cache_path(input_path: Path) -> Path:
    digest = hashlib.sha256(str(input_path.resolve()).casefold().encode("utf-8")).hexdigest()
    return get_app_data_directory() / "cache" / "finder" / f"{digest}.json"


def clear_finder_cache() -> int:
    """Delete all Finder JSON cache files and return the removed count."""

    directory = get_app_data_directory() / "cache" / "finder"
    removed = 0
    if not directory.is_dir():
        return removed
    for path in directory.glob("*.json"):
        try:
            path.unlink()
            removed += 1
        except OSError:
            LOGGER.warning("PDF Finder cache file could not be deleted: %s", path)
    return removed


def prune_finder_cache() -> int:
    """Remove expired entries and oldest files above the storage limit."""

    directory = get_app_data_directory() / "cache" / "finder"
    if not directory.is_dir():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=CACHE_RETENTION_DAYS)
    candidates: list[tuple[Path, float, int]] = []
    removed = 0
    for path in directory.glob("*.json"):
        try:
            stat = path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc)
            if modified < cutoff:
                path.unlink()
                removed += 1
            else:
                candidates.append((path, stat.st_mtime, stat.st_size))
        except OSError:
            LOGGER.warning("PDF Finder cache file could not be inspected: %s", path)
    total = sum(item[2] for item in candidates)
    for path, _modified, size in sorted(candidates, key=lambda item: item[1]):
        if total <= CACHE_MAX_BYTES:
            break
        try:
            path.unlink()
            total -= size
            removed += 1
        except OSError:
            LOGGER.warning("PDF Finder cache file could not be pruned: %s", path)
    return removed
