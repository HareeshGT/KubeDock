"""SQLite persistence for KubeDock's local application data.

The database is stored in ~/.vm_visualizer/kubedock.db.  SQLite is the active
store for application settings, recent instances, and dashboard history.
Legacy JSON/CSV files are read only once for migration and are never written by
normal application operations.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path

APP_DIR = Path.home() / ".vm_visualizer"
DB_PATH = APP_DIR / "kubedock.db"
LEGACY_SETTINGS_PATH = APP_DIR / "settings.json"
SETTINGS_MIGRATION_MARKER = "migration.settings_json"
_LOCK = threading.RLock()


def connect():
    """Open a short-lived SQLite connection configured for local app use."""
    APP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(APP_DIR, 0o700)
    except OSError:
        pass

    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")

    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass
    return conn


def _migrate_legacy_settings(db: sqlite3.Connection) -> None:
    """Import legacy settings.json into SQLite once, without modifying it.

    Existing SQLite settings win over legacy values so an already-in-use DB is
    never rolled back by a stale JSON file.  The marker is only written after a
    successful import (or when the legacy file is absent).
    """
    marker = db.execute(
        "SELECT 1 FROM app_settings WHERE setting_key=?",
        (SETTINGS_MIGRATION_MARKER,),
    ).fetchone()
    if marker:
        return

    source = LEGACY_SETTINGS_PATH
    if not source.exists():
        db.execute(
            "INSERT OR REPLACE INTO app_settings(setting_key, value_json, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP)",
            (SETTINGS_MIGRATION_MARKER, "true"),
        )
        return

    try:
        with source.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        # Leave the marker unset so a later application start can retry.
        return

    if not isinstance(data, dict):
        return

    for key, value in data.items():
        if not isinstance(key, str) or not key or key.startswith("migration."):
            continue
        db.execute(
            """INSERT OR IGNORE INTO app_settings(setting_key, value_json, updated_at)
               VALUES (?, ?, CURRENT_TIMESTAMP)""",
            (key, json.dumps(value, separators=(",", ":"))),
        )

    # Marker is written only after all values have been staged successfully.
    db.execute(
        "INSERT OR REPLACE INTO app_settings(setting_key, value_json, updated_at) "
        "VALUES (?, ?, CURRENT_TIMESTAMP)",
        (SETTINGS_MIGRATION_MARKER, "true"),
    )


def initialize() -> None:
    """Create the schema and perform one-time legacy settings migration."""
    with _LOCK, connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS recent_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT NOT NULL,
                port TEXT NOT NULL DEFAULT '22',
                user TEXT NOT NULL DEFAULT '',
                pem TEXT NOT NULL DEFAULT '',
                alias TEXT NOT NULL DEFAULT '',
                protocol TEXT NOT NULL DEFAULT 'ssh',
                last_used TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(host, port, user)
            );
            CREATE INDEX IF NOT EXISTS idx_recent_last_used
                ON recent_instances(last_used DESC, id DESC);
            CREATE TABLE IF NOT EXISTS dashboard_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_key TEXT NOT NULL,
                sample_time TEXT NOT NULL,
                cpu REAL,
                memory REAL,
                pods INTEGER,
                ready_nodes INTEGER,
                restarts INTEGER NOT NULL DEFAULT 0,
                pending INTEGER NOT NULL DEFAULT 0,
                events_json TEXT NOT NULL DEFAULT '[]',
                UNIQUE(instance_key, sample_time)
            );
            CREATE INDEX IF NOT EXISTS idx_dashboard_history_instance_time
                ON dashboard_history(instance_key, id DESC);
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        _migrate_legacy_settings(db)


def get_setting(key, default=None):
    """Read one application setting from SQLite."""
    initialize()
    with connect() as db:
        row = db.execute(
            "SELECT value_json FROM app_settings WHERE setting_key=?",
            (key,),
        ).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, json.JSONDecodeError, ValueError):
        return default


def get_all_settings() -> dict:
    """Read all user-facing application settings from SQLite."""
    initialize()
    with connect() as db:
        rows = db.execute(
            "SELECT setting_key, value_json FROM app_settings "
            "WHERE setting_key NOT LIKE 'migration.%'"
        ).fetchall()

    settings = {}
    for row in rows:
        try:
            settings[row[0]] = json.loads(row[1])
        except (TypeError, json.JSONDecodeError, ValueError):
            continue
    return settings


def set_setting(key, value):
    """Write one application setting to SQLite."""
    initialize()
    encoded = json.dumps(value, separators=(",", ":"))
    with _LOCK, connect() as db:
        db.execute(
            """INSERT INTO app_settings(setting_key,value_json,updated_at)
               VALUES(?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(setting_key) DO UPDATE SET
               value_json=excluded.value_json,
               updated_at=CURRENT_TIMESTAMP""",
            (key, encoded),
        )


def set_settings(values: dict) -> None:
    """Atomically write multiple application settings to SQLite."""
    if not values:
        return
    initialize()
    with _LOCK, connect() as db:
        for key, value in values.items():
            if not isinstance(key, str) or not key or key.startswith("migration."):
                continue
            db.execute(
                """INSERT INTO app_settings(setting_key,value_json,updated_at)
                   VALUES(?,?,CURRENT_TIMESTAMP)
                   ON CONFLICT(setting_key) DO UPDATE SET
                   value_json=excluded.value_json,
                   updated_at=CURRENT_TIMESTAMP""",
                (key, json.dumps(value, separators=(",", ":"))),
            )


initialize()
