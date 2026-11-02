"""Unit tests for the shared KubeDock connection service."""

from services.connection import ConnectionService
from services.runtime import AppServices


class FakeResource:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def test_attach_ssh_publishes_safe_snapshot_and_generation():
    ssh = FakeResource()
    sftp = FakeResource()
    service = ConnectionService(pool_closer=lambda _ssh: None)
    events = []
    service.subscribe(events.append)

    snapshot = service.attach_ssh(
        ssh, sftp, host="server.example", port=22,
        username="operator", alias="production",
    )

    assert snapshot.is_active
    assert snapshot.protocol == "ssh"
    assert snapshot.host == "server.example"
    assert snapshot.username == "operator"
    assert snapshot.alias == "production"
    assert snapshot.session_id
    assert snapshot.generation == 1
    assert service.get_ssh_client() is ssh
    assert service.get_filesystem() is sftp
    assert events[-1] == snapshot
    assert "password" not in snapshot.__dict__
    assert "private_key" not in snapshot.__dict__


def test_health_transitions_and_generation_validation():
    ssh = FakeResource()
    service = ConnectionService(pool_closer=lambda _ssh: None)
    initial = service.attach_ssh(ssh, host="node", username="user")

    degraded = service.mark_degraded("heartbeat missed")
    assert degraded.state == "degraded"
    assert degraded.is_active
    assert degraded.last_error == "heartbeat missed"
    assert service.is_current(initial.generation)

    recovered = service.mark_recovered(latency_ms=12.5)
    assert recovered.state == "connected"
    assert recovered.latency_ms == 12.5
    assert recovered.last_error is None

    lost = service.mark_lost("transport closed")
    assert lost.state == "lost"
    assert not lost.is_active
    assert service.get_ssh_client() is None
    assert not service.is_current(initial.generation)


def test_disconnect_closes_pool_filesystem_and_ssh_once():
    ssh = FakeResource()
    sftp = FakeResource()
    closed_pools = []
    service = ConnectionService(pool_closer=closed_pools.append)
    service.attach_ssh(ssh, sftp, host="node")

    disconnected = service.disconnect()
    assert disconnected.state == "disconnected"
    assert disconnected.generation == 2
    assert closed_pools == [ssh]
    assert sftp.close_calls == 1
    assert ssh.close_calls == 1

    # Repeated shutdown is idempotent and cannot double-close resources.
    assert service.disconnect() == disconnected
    assert closed_pools == [ssh]
    assert sftp.close_calls == 1
    assert ssh.close_calls == 1


def test_ftp_session_has_no_ssh_client():
    filesystem = FakeResource()
    service = ConnectionService()
    snapshot = service.attach_file_connection(
        filesystem, protocol="ftps", host="files.example", port=990,
        username="transfer",
    )

    assert snapshot.protocol == "ftps"
    assert snapshot.is_active
    assert service.get_ssh_client() is None
    assert service.get_filesystem() is filesystem
    service.disconnect()
    assert filesystem.close_calls == 1


def test_replacing_session_closes_previous_resources_but_not_reused_ones():
    first_ssh, second_ssh = FakeResource(), FakeResource()
    first_fs, second_fs = FakeResource(), FakeResource()
    closed_pools = []
    service = ConnectionService(pool_closer=closed_pools.append)
    service.attach_ssh(first_ssh, first_fs, host="one")
    second = service.attach_ssh(second_ssh, second_fs, host="two")

    assert second.generation == 2
    assert closed_pools == [first_ssh]
    assert first_fs.close_calls == 1
    assert first_ssh.close_calls == 1
    assert second_fs.close_calls == 0
    assert second_ssh.close_calls == 0
    assert service.get_ssh_client() is second_ssh


def test_subscriber_unsubscribe_and_listener_failure_are_isolated():
    service = ConnectionService(pool_closer=lambda _ssh: None)
    events = []
    token = service.subscribe(events.append)
    broken = service.subscribe(lambda _snapshot: (_ for _ in ()).throw(RuntimeError("boom")))
    service.unsubscribe(token)

    service.attach_ssh(FakeResource(), host="node")
    assert len(events) == 1  # initial disconnected snapshot only
    service.unsubscribe(broken)


def test_app_services_shutdown_is_idempotent():
    ssh = FakeResource()
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    connection.attach_ssh(ssh, host="node")
    app_services = AppServices(connection)

    app_services.shutdown()
    app_services.shutdown()

    assert app_services.stopped
    assert ssh.close_calls == 1
    assert connection.snapshot.state == "disconnected"
