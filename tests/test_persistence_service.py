"""Persistence facade tests without touching the user's SQLite profile."""

from services.lifecycle import ServiceLifecycleManager, ServiceState
from services.persistence import SettingsService


class FakeDatabase:
    def __init__(self):
        self.values = {"theme": "App Default"}
        self.saved = []

    def get_all_settings(self):
        return dict(self.values)

    def get_setting(self, key, default=None):
        return self.values.get(key, default)

    def set_setting(self, key, value):
        self.values[key] = value

    def set_settings(self, values):
        self.saved.append(dict(values))
        self.values.update(values)


def test_settings_service_uses_shared_database_and_lazy_lifecycle():
    manager = ServiceLifecycleManager()
    database = FakeDatabase()
    settings = SettingsService(database)
    manager.register("database", database)
    manager.register("settings", settings, dependencies=("database",))
    database._lifecycle_manager = manager
    database._lifecycle_name = "database"
    settings._lifecycle_manager = manager
    settings._lifecycle_name = "settings"

    assert manager.status("settings").state == ServiceState.STOPPED
    assert settings.load() == {"theme": "App Default"}
    assert settings.get("missing", "fallback") == "fallback"
    settings.save(refresh_interval=60)

    assert database.saved == [{"refresh_interval": 60}]
    assert manager.status("database").state == ServiceState.RUNNING
    assert manager.status("settings").state == ServiceState.RUNNING
