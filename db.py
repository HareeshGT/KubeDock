"""SQLite persistence for KubeDock's local application data.

KubeDock keeps its existing database in ~/.kubedock when that location is
available. Frozen installs can run with a different or restricted home
directory, so the connection layer falls back to the operating system's
per-user application-data directory if the legacy location cannot be opened.
Legacy JSON/CSV files remain read-only migration sources.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

_HOME_DIR = None
try:
    _HOME_DIR = Path.home()
except (OSError, RuntimeError):
    pass

# Keep the previous location as the first choice so upgrades do not silently
# start a second, empty profile for users whose existing directory is healthy.
LEGACY_APP_DIR = _HOME_DIR / ".kubedock" if _HOME_DIR else None
LEGACY_DB_PATH = LEGACY_APP_DIR / "kubedock.db" if LEGACY_APP_DIR else None
LEGACY_SETTINGS_PATH = LEGACY_APP_DIR / "settings.json" if LEGACY_APP_DIR else Path(".kubedock/settings.json")

APP_DIR = LEGACY_APP_DIR or Path(".kubedock")
DB_PATH = APP_DIR / "kubedock.db"
SETTINGS_MIGRATION_MARKER = "migration.settings_json"
_LOCK = threading.RLock()


def _candidate_app_dirs() -> list[Path]:
    """Return preferred, then platform-native writable locations for app data."""
    candidates: list[Path] = []
    override = os.environ.get("KUBEDOCK_DATA_DIR", "").strip()
    if override:
        candidates.append(Path(override).expanduser())

    if LEGACY_APP_DIR is not None:
        candidates.append(LEGACY_APP_DIR)

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            candidates.append(Path(local_app_data) / "KubeDock")

        roaming_app_data = os.environ.get("APPDATA", "").strip()
        if roaming_app_data:
            candidates.append(Path(roaming_app_data) / "KubeDock")

        if _HOME_DIR is not None:
            candidates.extend([
                _HOME_DIR / "AppData" / "Local" / "KubeDock",
                _HOME_DIR / "AppData" / "Roaming" / "KubeDock",
                _HOME_DIR / "KubeDockData",
            ])

        # An existing LocalAppData\\KubeDock directory can have broken ACLs
        # (for example, after an elevated install). A separate user temp
        # directory is a final writable fallback so startup does not fail only
        # because that old directory cannot be opened. Existing DB data is
        # copied here when the legacy database remains readable.
        try:
            temp_root = Path(tempfile.gettempdir())
            user_key = os.environ.get("USERNAME", "").strip() or os.environ.get("USER", "").strip() or "current-user"
            safe_user_key = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in user_key)[:48]
            candidates.append(temp_root / "KubeDock-Data" / safe_user_key)
        except (OSError, RuntimeError):
            pass
    elif sys.platform == "darwin":
        if _HOME_DIR is not None:
            candidates.append(_HOME_DIR / "Library" / "Application Support" / "KubeDock")
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg_data_home and Path(xdg_data_home).is_absolute():
            candidates.append(Path(xdg_data_home) / "KubeDock")
        elif _HOME_DIR is not None:
            candidates.append(_HOME_DIR / ".local" / "share" / "KubeDock")

    # De-duplicate without changing precedence.
    return list(dict.fromkeys(candidates))


def _is_storage_path_error(exc: sqlite3.OperationalError) -> bool:
    """Only retry another directory for path/permission failures, not DB locks or corruption."""
    message = str(exc).casefold()
    return any(
        marker in message
        for marker in (
            "unable to open database file",
            "attempt to write a readonly database",
            "readonly database",
            "disk i/o error",
        )
    )


def _restore_legacy_database_if_needed(
    conn: sqlite3.Connection,
    directory: Path,
    database_already_exists: bool,
) -> None:
    """Copy the existing SQLite database into a fallback directory if needed."""
    if database_already_exists or LEGACY_DB_PATH is None or not LEGACY_DB_PATH.is_file():
        return
    if LEGACY_APP_DIR is not None and directory == LEGACY_APP_DIR:
        return

    source = None
    try:
        source_uri = LEGACY_DB_PATH.resolve().as_uri() + "?mode=ro"
        source = sqlite3.connect(source_uri, uri=True, timeout=3)
        source.backup(conn)
    except (OSError, sqlite3.Error):
        # A failed best-effort backup must not prevent a fresh profile from
        # starting; settings.json migration below still uses its original path.
        pass
    finally:
        if source is not None:
            source.close()


def connect():
    """Open a SQLite connection, falling back when the preferred data path is unusable."""
    global APP_DIR, DB_PATH

    failures: list[str] = []
    for directory in _candidate_app_dirs():
        conn = None
        try:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass

            database_path = directory / "kubedock.db"
            try:
                database_already_exists = database_path.is_file() and database_path.stat().st_size > 0
            except OSError:
                database_already_exists = False

            conn = sqlite3.connect(str(database_path), timeout=10)
            _restore_legacy_database_if_needed(conn, directory, database_already_exists)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 10000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")

            try:
                os.chmod(database_path, 0o600)
            except OSError:
                pass

            APP_DIR = directory
            DB_PATH = database_path
            return conn

        except OSError as exc:
            failures.append(f"{directory}: {exc}")
            if conn is not None:
                conn.close()
        except sqlite3.OperationalError as exc:
            if conn is not None:
                conn.close()
            if not _is_storage_path_error(exc):
                raise
            failures.append(f"{directory}: {exc}")

    tried = "; ".join(failures) if failures else "no usable home or application-data directory was found"
    raise sqlite3.OperationalError(
        "KubeDock could not open its SQLite database. Tried the legacy profile and "
        "platform application-data locations. Check the user's directory permissions "
        "or set KUBEDOCK_DATA_DIR to a writable folder. Details: " + tried
    )


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
