from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class SQLiteCache:
    """Small thread-safe JSON cache shared by provider adapters."""

    def __init__(self, path: str | Path = "data/cache.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._initialize()

    @staticmethod
    def make_key(namespace: str, **parameters: Any) -> str:
        payload = json.dumps(parameters, sort_keys=True, default=str, ensure_ascii=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        return f"{namespace}:{digest}"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS cache_entries (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def get(self, key: str) -> Any | None:
        now = datetime.now(UTC)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value, expires_at FROM cache_entries WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if datetime.fromisoformat(row[1]) <= now:
                connection.execute("DELETE FROM cache_entries WHERE key = ?", (key,))
                return None
            return json.loads(row[0])

    def get_many(self, keys: list[str]) -> dict[str, Any]:
        unique_keys = list(dict.fromkeys(keys))
        if not unique_keys:
            return {}
        now = datetime.now(UTC)
        placeholders = ", ".join("?" for _ in unique_keys)
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT key, value, expires_at FROM cache_entries "
                f"WHERE key IN ({placeholders})",
                unique_keys,
            ).fetchall()
            expired = [
                str(row[0])
                for row in rows
                if datetime.fromisoformat(str(row[2])) <= now
            ]
            if expired:
                expired_placeholders = ", ".join("?" for _ in expired)
                connection.execute(
                    f"DELETE FROM cache_entries "
                    f"WHERE key IN ({expired_placeholders})",
                    expired,
                )
            return {
                str(row[0]): json.loads(str(row[1]))
                for row in rows
                if str(row[0]) not in expired
            }

    def set(self, key: str, value: Any, ttl: timedelta) -> None:
        now = datetime.now(UTC)
        expires_at = now + ttl
        payload = json.dumps(value, default=str, ensure_ascii=False)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cache_entries(key, value, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at
                """,
                (key, payload, expires_at.isoformat(), now.isoformat()),
            )

    def set_many(self, values: dict[str, Any], ttl: timedelta) -> None:
        if not values:
            return
        now = datetime.now(UTC)
        expires_at = now + ttl
        rows = [
            (
                key,
                json.dumps(value, default=str, ensure_ascii=False),
                expires_at.isoformat(),
                now.isoformat(),
            )
            for key, value in values.items()
        ]
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO cache_entries(key, value, expires_at, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    expires_at = excluded.expires_at,
                    created_at = excluded.created_at
                """,
                rows,
            )

    def purge_expired(self) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM cache_entries WHERE expires_at <= ?",
                (datetime.now(UTC).isoformat(),),
            )
            return cursor.rowcount
