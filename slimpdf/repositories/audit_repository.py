"""Transactional, tamper-evident audit storage for PDF security removal."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

AUDIT_RETENTION_DAYS = 365
_KEY_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()


class AuditIntegrityError(RuntimeError):
    """Raised when an audit record or its HMAC chain does not verify."""


def sha256_file(path: Path) -> str:
    """Hash a potentially large file without loading it into memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


class UnlockAuditRepository:
    """Serialize audit writes with SQLite and verify a keyed HMAC chain."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.key_path = path.with_suffix(path.suffix + ".key")

    def append(self, event: dict[str, Any]) -> None:
        """Append one event in an immediate transaction after chain verification."""

        with _WRITE_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            key = self._load_or_create_key()
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                records = self._read_and_verify(connection, key)
                cutoff = datetime.now(timezone.utc) - timedelta(days=AUDIT_RETENTION_DAYS)
                retained = [record for record in records if self._timestamp(record) >= cutoff]
                if len(retained) != len(records):
                    connection.execute("DELETE FROM unlock_events")
                    previous = ""
                    for record in retained:
                        record.pop("record_hmac", None)
                        record["previous_record_hmac"] = previous
                        previous = self._insert_record(connection, key, record, previous)
                    records = retained
                previous_hmac = records[-1]["record_hmac"] if records else ""
                sanitized = dict(event)
                sanitized["previous_record_hmac"] = previous_hmac
                self._insert_record(connection, key, sanitized, previous_hmac)
                connection.commit()

    def read_records(self) -> tuple[dict[str, Any], ...]:
        """Return verified records and fail loudly on corruption or chain mismatch."""

        if not self.path.is_file():
            return ()
        key = self._load_or_create_key(create=False)
        with closing(self._connect()) as connection:
            return tuple(self._read_and_verify(connection, key))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS unlock_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, executed_at_utc TEXT NOT NULL, "
            "payload_json TEXT NOT NULL, previous_hmac TEXT NOT NULL, record_hmac TEXT NOT NULL)"
        )
        return connection

    def _read_and_verify(
        self, connection: sqlite3.Connection, key: bytes
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT payload_json, previous_hmac, record_hmac FROM unlock_events ORDER BY id"
        ).fetchall()
        records: list[dict[str, Any]] = []
        previous = ""
        for payload, stored_previous, stored_hmac in rows:
            if stored_previous != previous:
                raise AuditIntegrityError("PDF 보안 해제 감사 기록 연결이 손상되었습니다.")
            expected = hmac.new(
                key, str(payload).encode("utf-8"), hashlib.sha256
            ).hexdigest().upper()
            if not hmac.compare_digest(expected, str(stored_hmac)):
                raise AuditIntegrityError("PDF 보안 해제 감사 기록 무결성 검증에 실패했습니다.")
            try:
                record = json.loads(payload)
            except (TypeError, json.JSONDecodeError) as error:
                raise AuditIntegrityError("PDF 보안 해제 감사 기록 JSON이 손상되었습니다.") from error
            if not isinstance(record, dict):
                raise AuditIntegrityError("PDF 보안 해제 감사 기록 형식이 올바르지 않습니다.")
            if record.get("previous_record_hmac", "") != stored_previous:
                raise AuditIntegrityError("PDF 보안 해제 감사 기록 내부 연결값이 손상되었습니다.")
            record["record_hmac"] = stored_hmac
            records.append(record)
            previous = str(stored_hmac)
        return records

    @staticmethod
    def _timestamp(record: dict[str, Any]) -> datetime:
        try:
            value = datetime.fromisoformat(str(record["executed_at_utc"]))
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        except (KeyError, TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    @staticmethod
    def _insert_record(
        connection: sqlite3.Connection,
        key: bytes,
        record: dict[str, Any],
        previous_hmac: str,
    ) -> str:
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        record_hmac = hmac.new(
            key, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest().upper()
        connection.execute(
            "INSERT INTO unlock_events(executed_at_utc, payload_json, previous_hmac, record_hmac) "
            "VALUES (?, ?, ?, ?)",
            (str(record.get("executed_at_utc", "")), payload, previous_hmac, record_hmac),
        )
        record["record_hmac"] = record_hmac
        return record_hmac

    def _load_or_create_key(self, *, create: bool = True) -> bytes:
        with _KEY_LOCK:
            if self.key_path.is_file():
                key = self.key_path.read_bytes()
                if len(key) != 32:
                    raise AuditIntegrityError("감사 기록 HMAC 키가 손상되었습니다.")
                return key
            if not create:
                raise AuditIntegrityError("감사 기록 HMAC 키를 찾을 수 없습니다.")
            key = os.urandom(32)
            binary_flag = getattr(os, "O_BINARY", 0)
            descriptor = os.open(
                self.key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | binary_flag,
                0o600,
            )
            try:
                os.write(descriptor, key)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            if len(key) != 32:
                raise AuditIntegrityError("감사 기록 HMAC 키가 손상되었습니다.")
            return key
