"""Database service lifecycle tests."""

from services.lifecycle import ServiceLifecycleManager, ServiceState
from services.persistence import DatabaseService


def test_database_lifecycle_hook_initializes_without_recursive_activation(monkeypatch):
    import db

    calls = []
    monkeypatch.setattr(db, "initialize", lambda: calls.append("initialize"))

    manager = ServiceLifecycleManager()
    database = DatabaseService()
    manager.register("database", database)
    database._lifecycle_manager = manager
    database._lifecycle_name = "database"

    status = manager.start("database")

    assert calls == ["initialize"]
    assert status.state == ServiceState.RUNNING
