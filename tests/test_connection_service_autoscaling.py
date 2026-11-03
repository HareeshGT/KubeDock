"""ConnectionService integration tests for SSH autoscaling policy."""


from services.connection import ConnectionService


class FakeResource:
    def close(self):
        pass


class FakePool:
    def __init__(self):
        self.config = {}
        self.closed = False

    def configure(self, **kwargs):
        self.config.update(kwargs)

    def snapshot(self):
        return {
            "total_connections": 3,
            "max_connections": self.config.get("max_total_connections", 5),
            "secondary_connections": 2,
            "active_channels": 7,
            "channel_capacity": 12,
            "channels_per_connection": 6,
            "scale_up_utilization": 0.70,
            "idle_timeout_seconds": 60.0,
            "closed": self.closed,
        }


def test_connection_service_applies_five_connection_policy_and_exposes_metrics():
    ssh = FakeResource()
    pool = FakePool()
    ssh._kdb_connection_pool = pool
    service = ConnectionService(max_ssh_connections=5, pool_closer=lambda _ssh: None)

    service.attach_ssh(ssh, host="server.example")

    assert pool.config == {"max_total_connections": 5}
    assert service.pool_status() == {
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


def test_connection_service_never_allows_more_than_five_total_transports():
    service = ConnectionService(max_ssh_connections=99)
    assert service.max_ssh_connections == 5

    lower = ConnectionService(max_ssh_connections=2)
    assert lower.max_ssh_connections == 2
