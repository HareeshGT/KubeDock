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


def test_pool_teardown_failure_does_not_skip_primary_resource_cleanup():
    ssh = FakeResource()
    filesystem = FakeResource()

    def broken_pool_close(_ssh):
        raise RuntimeError("pool close failed")

    service = ConnectionService(pool_closer=broken_pool_close)
    service.attach_ssh(ssh, filesystem, host="node")
    service.disconnect()

    assert filesystem.close_calls == 1
    assert ssh.close_calls == 1


def test_reused_filesystem_is_not_closed_when_replacing_ssh_client():
    first_ssh, second_ssh = FakeResource(), FakeResource()
    shared_filesystem = FakeResource()
    service = ConnectionService(pool_closer=lambda _ssh: None)
    service.attach_ssh(first_ssh, shared_filesystem, host="one")
    service.attach_ssh(second_ssh, shared_filesystem, host="two")

    assert first_ssh.close_calls == 1
    assert shared_filesystem.close_calls == 0
    assert service.get_filesystem() is shared_filesystem


def test_app_services_compose_domain_services_over_one_connection():
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    services = AppServices(connection)

    assert services.connection is connection
    assert services.commands.connection is connection
    assert services.kubernetes.connection is connection
    assert services.docker.connection is connection
    assert services.dashboard.connection is connection
    assert services.files.connection is connection
    assert services.terminal.connection is connection
    assert services.webapp.connection is connection
    assert services.webapp.commands is services.commands
    assert services.kubernetes.commands is services.commands
    assert services.docker.commands is services.commands
    assert services.dashboard.commands is services.commands
    assert services.files.commands is services.commands
    assert services.terminal.commands is services.commands


def test_webapp_provider_tracks_the_current_shared_session():
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    services = AppServices(connection)
    first = FakeResource()
    second = FakeResource()

    connection.attach_ssh(first, host="first.example")
    assert services.webapp.ssh_provider() is first

    connection.attach_ssh(second, host="second.example")
    assert services.webapp.ssh_provider() is second
    assert first.close_calls == 1

    connection.disconnect()
    assert services.webapp.ssh_provider() is None
    assert second.close_calls == 1


def test_command_service_rejects_execution_without_active_ssh():
    connection = ConnectionService()
    services = AppServices(connection)

    import pytest
    with pytest.raises(RuntimeError, match="Connect to an SSH host"):
        services.commands.create_worker(None, "uname -a")


class FakeFilesystem(FakeResource):
    def __init__(self):
        super().__init__()
        self.calls = []

    def normalize(self, path):
        self.calls.append(("normalize", path))
        return path or "/"

    def mkdir(self, path):
        self.calls.append(("mkdir", path))

    def write_bytes(self, path, payload):
        self.calls.append(("write_bytes", path, payload))

    def rmdir(self, path):
        self.calls.append(("rmdir", path))

    def remove(self, path):
        self.calls.append(("remove", path))

    def rename(self, old_path, new_path):
        self.calls.append(("rename", old_path, new_path))


def test_connection_session_getters_return_matching_snapshot_atomically():
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    first, second = FakeResource(), FakeResource()

    first_snapshot = connection.attach_ssh(first, host="first")
    client, snapshot = connection.get_ssh_session()
    assert client is first
    assert snapshot == first_snapshot

    second_snapshot = connection.attach_ssh(second, host="second")
    client, snapshot = connection.get_ssh_session()
    assert client is second
    assert snapshot == second_snapshot
    assert snapshot.generation == first_snapshot.generation + 1


def test_file_manager_service_routes_filesystem_operations_through_connection():
    connection = ConnectionService()
    filesystem = FakeFilesystem()
    connection.attach_file_connection(
        filesystem, protocol="ftp", host="files.example", port=22,
        username="operator",
    )
    services = AppServices(connection)

    assert services.files.normalize("/var/log") == "/var/log"
    services.files.mkdir("/var/log/new")
    services.files.create_empty_file("/var/log/new/empty.txt")
    services.files.delete("/var/log/old.txt")
    services.files.delete("/var/log/old-dir", is_directory=True)
    services.files.rename("/var/log/a", "/var/log/b")

    assert filesystem.calls == [
        ("normalize", "/var/log"),
        ("mkdir", "/var/log/new"),
        ("write_bytes", "/var/log/new/empty.txt", b""),
        ("remove", "/var/log/old.txt"),
        ("rmdir", "/var/log/old-dir"),
        ("rename", "/var/log/a", "/var/log/b"),
    ]


def test_command_worker_discards_result_after_connection_generation_changes(monkeypatch):
    import pytest
    import workers_parts.command as command_parts

    connection = ConnectionService(pool_closer=lambda _ssh: None)
    first = FakeResource()
    services = AppServices(connection)
    original = connection.attach_ssh(first, host="first.example")
    worker = services.commands.create_worker(None, "uname -a")
    errors = []
    worker.error.connect(errors.append)

    connection.attach_ssh(FakeResource(), host="second.example")
    assert worker.connection_generation == original.generation
    assert worker.connection_session_id == original.session_id
    assert not worker._session_is_current()

    monkeypatch.setattr(
        command_parts,
        "managed_exec_command",
        lambda *_args, **_kwargs: pytest.fail("stale worker must not open an SSH channel"),
    )
    worker.run()

    assert errors and "active connection changed" in errors[0].lower()


class FakeDirectoryFilesystem(FakeFilesystem):
    def listdir_attr(self, path):
        self.calls.append(("listdir_attr", path))
        return []


def test_directory_worker_does_not_read_after_session_replacement():
    connection = ConnectionService()
    first = FakeDirectoryFilesystem()
    connection.attach_file_connection(first, protocol="ftp", host="first.example")
    services = AppServices(connection)
    worker = services.files.create_directory_worker(None, "/")

    connection.attach_file_connection(
        FakeDirectoryFilesystem(), protocol="ftp", host="second.example",
    )
    worker.run()

    assert ("listdir_attr", "/") not in first.calls


def test_file_stream_reader_rejects_a_superseded_filesystem_session():
    connection = ConnectionService()
    first = FakeFilesystem()
    connection.attach_file_connection(first, protocol="ftp", host="first.example")
    services = AppServices(connection)
    worker = services.files.create_stream_reader("/tmp/test.txt")
    errors = []
    worker.finished_err.connect(errors.append)

    connection.attach_file_connection(
        FakeFilesystem(), protocol="ftp", host="second.example",
    )
    worker.run()

    assert errors and "connection changed" in errors[0].lower()
    assert first.calls == []


def test_connection_service_owns_connection_and_health_worker_factories():
    from workers import ConnectWorker, FTPConnectionWorker, ConnectionHealthWorker

    connection = ConnectionService(pool_closer=lambda _ssh: None)
    ssh = connection.create_ssh_connect_worker(
        "ssh.example", 22, "operator", "/tmp/operator.pem", "",
    )
    ftp = connection.create_file_connect_worker(
        "ftp.example", 21, "operator", "secret", tls=False, passive=True,
    )
    assert isinstance(ssh, ConnectWorker)
    assert isinstance(ftp, FTPConnectionWorker)

    connection.attach_ssh(FakeResource(), host="ssh.example")
    health = connection.create_health_worker()
    assert isinstance(health, ConnectionHealthWorker)
    assert health.connection_generation == connection.snapshot.generation


class FakeSSHWithChannels(FakeResource):
    def __init__(self):
        super().__init__()
        self.sftp_channel = FakeResource()
        self.terminal_channel = FakeResource()
        self.terminal_options = None

    def open_sftp(self):
        return self.sftp_channel

    def invoke_shell(self, **kwargs):
        self.terminal_options = kwargs
        return self.terminal_channel


def test_webapp_service_opens_sftp_and_terminal_channels_on_shared_session():
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    services = AppServices(connection)
    ssh = FakeSSHWithChannels()
    connection.attach_ssh(ssh, host="shared.example")

    assert services.webapp.open_sftp() is ssh.sftp_channel
    assert services.webapp.open_terminal_channel(
        term="xterm-256color", width=100, height=28,
    ) is ssh.terminal_channel
    assert ssh.terminal_options == {
        "term": "xterm-256color", "width": 100, "height": 28,
    }


def test_webapp_service_rejects_sftp_and_terminal_without_active_ssh():
    connection = ConnectionService()
    services = AppServices(connection)

    import pytest
    with pytest.raises(RuntimeError, match="Connect to an SSH host"):
        services.webapp.open_sftp()
    with pytest.raises(RuntimeError, match="active SSH"):
        services.webapp.open_terminal_channel()

def test_docker_log_stream_worker_is_bound_to_shared_connection_generation():
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    services = AppServices(connection)
    first = FakeResource()
    original = connection.attach_ssh(first, host="first.example")

    worker = services.docker.create_log_stream_worker("docker logs -f app")
    assert worker.connection_generation == original.generation
    assert worker._session_is_current()

    connection.attach_ssh(FakeResource(), host="second.example")
    assert not worker._session_is_current()
