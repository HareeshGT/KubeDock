"""SQLite and application-settings adapters owned by AppServices."""

from __future__ import annotations

from .lifecycle import activate_lifecycle


class DatabaseService:
    """Thin lifecycle-aware access to KubeDock's existing SQLite store.

    Connections remain short-lived and are created by db.py so its platform
    path selection, WAL settings, migrations, and permission handling remain
    the single source of truth.
    """

    def initialize(self):
        # Invoked by ServiceLifecycleManager.start(); do not recursively
        # activate the same service while it is in STARTING state.
        import db
        db.initialize()

    def connect(self):
        activate_lifecycle(self)
        import db
        return db.connect()

    def get_setting(self, key, default=None):
        activate_lifecycle(self)
        import db
        return db.get_setting(key, default)

    def get_all_settings(self):
        activate_lifecycle(self)
        import db
        return db.get_all_settings()

    def set_setting(self, key, value):
        activate_lifecycle(self)
        import db
        return db.set_setting(key, value)

    def set_settings(self, values):
        activate_lifecycle(self)
        import db
        return db.set_settings(values)

    @property
    def path(self):
        import db
        return str(db.DB_PATH)


class SettingsService:
    """Application settings facade backed by the existing SQLite table."""

    def __init__(self, database):
        self.database = database

    def load(self):
        activate_lifecycle(self)
        return self.database.get_all_settings()

    def get(self, key, default=None):
        activate_lifecycle(self)
        return self.database.get_setting(key, default)

    def save(self, **values):
        activate_lifecycle(self)
        self.database.set_settings(values)
        # Keep the existing theme palette in sync when a caller saves a theme.
        theme = values.get("theme")
        if theme:
            import themes
            themes.apply_theme_vars(theme)

    def save_current_theme(self):
        """Persist the currently selected theme using the legacy facade."""
        activate_lifecycle(self)
        import themes
        self.database.set_setting("theme", themes.CURRENT_THEME)
