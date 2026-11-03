"""Tests for the application connection overview shown on the Dashboard."""

from types import SimpleNamespace

from services.dashboard import DashboardService


class FakeConnection:
    snapshot = SimpleNamespace(
        state="connected",
        protocol="ssh",
        host="server.example",
        port=22,
        username="operator",
        alias="production",
        generation=4,
        connected_at=1_800_000_000.0,
        latency_ms=18.5,
        last_error=None,
    )

    def pool_status(self):
        return {
            "total_connections": 3,
            "max_connections": 5,
            "secondary_connections": 2,
            "active_channels": 7,
            "channel_capacity": 12,
            "channels_per_connection": 6,
            "scale_up_utilization": 0.70,
            "idle_timeout_seconds": 60.0,
            "closed": False,
        }


class FakeCommands:
    def worker_counts_by_service(self):
        return {"kubernetes": 2, "docker": 1, "terminal": 1}

    def active_operation_counts_by_service(self):
        return {"kubernetes": 1, "docker": 1}


def test_connection_overview_contains_safe_session_and_pool_metrics():
    result = DashboardService(FakeConnection(), FakeCommands()).connection_overview()

    assert result["state"] == "connected"
    assert result["protocol"] == "ssh"
    assert result["host"] == "server.example"
    assert result["port"] == 22
    assert result["username"] == "operator"
    assert result["alias"] == "production"
    assert result["generation"] == 4
    assert result["latency_ms"] == 18.5
    assert result["pool"]["total_connections"] == 3
    assert result["pool"]["max_connections"] == 5
    assert result["active_operations"] == 2
    assert result["registered_workers"] == 4
    assert "password" not in result
    assert "private_key" not in result


def test_connection_overview_handles_disconnected_and_missing_services():
    class DisconnectedConnection:
        snapshot = SimpleNamespace(state="disconnected", generation=2)

        def pool_status(self):
            return {"total_connections": 0, "max_connections": 5, "closed": True}

    result = DashboardService(DisconnectedConnection(), object()).connection_overview()

    assert result["state"] == "disconnected"
    assert result["protocol"] == "—"
    assert result["host"] == "—"
    assert result["pool"]["total_connections"] == 0
    assert result["active_operations"] == 0
    assert result["registered_workers"] == 0
