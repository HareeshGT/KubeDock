"""Regression tests for SQLite app-data path recovery in frozen installs."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import db


class DatabasePathFallbackTests(unittest.TestCase):
    def test_connect_falls_back_when_preferred_path_is_not_a_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            blocked = root / "blocked-profile"
            blocked.write_text("this file prevents directory creation", encoding="utf-8")
            fallback = root / "writable-profile"

            with (
                patch.object(db, "_candidate_app_dirs", return_value=[blocked, fallback]),
                patch.object(db, "LEGACY_APP_DIR", blocked),
                patch.object(db, "LEGACY_DB_PATH", root / "missing-legacy.db"),
                patch.object(db, "APP_DIR", blocked),
                patch.object(db, "DB_PATH", blocked / "kubedock.db"),
            ):
                connection = db.connect()
                try:
                    self.assertEqual(db.APP_DIR, fallback)
                    self.assertEqual(db.DB_PATH, fallback / "kubedock.db")
                    connection.execute(
                        "CREATE TABLE fallback_check (value TEXT NOT NULL)"
                    )
                    connection.execute(
                        "INSERT INTO fallback_check(value) VALUES (?)", ("writable",)
                    )
                    self.assertEqual(
                        connection.execute("SELECT value FROM fallback_check").fetchone()[0],
                        "writable",
                    )
                finally:
                    connection.close()

    def test_fallback_restores_existing_database_without_losing_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_dir = root / "old-profile"
            legacy_dir.mkdir()
            legacy_db = legacy_dir / "kubedock.db"
            source = sqlite3.connect(legacy_db)
            try:
                source.execute("CREATE TABLE preserved_data (value TEXT NOT NULL)")
                source.execute(
                    "INSERT INTO preserved_data(value) VALUES (?)", ("keep-existing-data",)
                )
                source.commit()
            finally:
                source.close()

            blocked = root / "blocked-profile"
            blocked.write_text("this file prevents directory creation", encoding="utf-8")
            fallback = root / "new-profile"

            with (
                patch.object(db, "_candidate_app_dirs", return_value=[blocked, fallback]),
                patch.object(db, "LEGACY_APP_DIR", blocked),
                patch.object(db, "LEGACY_DB_PATH", legacy_db),
                patch.object(db, "APP_DIR", blocked),
                patch.object(db, "DB_PATH", blocked / "kubedock.db"),
            ):
                connection = db.connect()
                try:
                    row = connection.execute(
                        "SELECT value FROM preserved_data"
                    ).fetchone()
                    self.assertEqual(row[0], "keep-existing-data")
                finally:
                    connection.close()


if __name__ == "__main__":
    unittest.main()
