"""Small, durable audit trail for KubeDock Web mutations.

Only action metadata is persisted. Command text, command output, credentials,
resource YAML, and Kubernetes Secret values are deliberately excluded.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional

_DEFAULT_DB = Path(os.path.expanduser("~")) / ".kubedock" / "web_operations.sqlite3"
_LOCK = threading.RLock()


def _connect(db_path: Optional[str | os.PathLike[str]] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else _DEFAULT_DB
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            username TEXT NOT NULL,
            category TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('succeeded', 'failed')),
            duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
            error_code INTEGER
        )"""
    )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_operations_timestamp ON operations(timestamp DESC)")
    return connection


def record_operation(*, username: str, category: str, action: str, target: str,
                     status: str, duration_ms: int, error_code: Optional[int] = None,
                     db_path: Optional[str | os.PathLike[str]] = None) -> None:
    """Persist bounded action metadata; never store command output or secrets."""
    if status not in {"succeeded", "failed"}:
        raise ValueError("status must be 'succeeded' or 'failed'")
    values = (str(username or "-")[:128], str(category)[:64], str(action)[:64],
              str(target)[:512], status, max(0, int(duration_ms)),
              int(error_code) if error_code is not None else None)
    with _LOCK:
        connection = _connect(db_path)
        try:
            connection.execute(
                """INSERT INTO operations
                   (username, category, action, target, status, duration_ms, error_code)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""", values)
            connection.execute(
                "DELETE FROM operations WHERE id NOT IN "
                "(SELECT id FROM operations ORDER BY id DESC LIMIT 5000)")
            connection.commit()
        finally:
            connection.close()


def list_operations(limit: int = 50, db_path: Optional[str | os.PathLike[str]] = None) -> list[dict]:
    """Read recent operation metadata; never return command output or secrets."""
    bounded_limit = max(1, min(int(limit), 200))
    with _LOCK:
        connection = _connect(db_path)
        try:
            rows = connection.execute(
                """SELECT id, timestamp, username, category, action, target,
                          status, duration_ms, error_code
                   FROM operations ORDER BY id DESC LIMIT ?""", (bounded_limit,)).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
