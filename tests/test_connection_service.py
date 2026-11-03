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
    assert services.ai.commands is services.commands
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

    replacement = FakeResource()
    replacement_snapshot = connection.attach_ssh(
        replacement, host="second.example",
    )
    replacement_worker = services.commands.create_worker(None, "uname -a")
    assert replacement_worker._session_is_current()
    assert replacement_worker.connection_generation == replacement_snapshot.generation
    assert replacement_worker.connection_session_id == replacement_snapshot.session_id


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
    with pytest.raises(RuntimeError, match="Connect to an SSH host"):
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

def test_kubernetes_pod_exec_worker_is_bound_to_shared_connection_generation():
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    services = AppServices(connection)
    original = connection.attach_ssh(FakeResource(), host="cluster-jump.example")

    worker = services.kubernetes.create_pod_exec_stream_worker(
        "default", "api-0", "api", "dev-context", cols=100, rows=30,
    )
    assert worker.connection_generation == original.generation
    assert worker.connection_session_id == original.session_id
    assert worker._session_is_current()

    connection.attach_ssh(FakeResource(), host="replacement.example")
    assert not worker._session_is_current()

def test_file_execution_stream_worker_is_bound_to_shared_connection_generation():
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    services = AppServices(connection)
    original = connection.attach_ssh(FakeResource(), host="file-host.example")

    worker = services.files.create_exec_stream_worker("python3 /tmp/job.py")
    assert worker.connection_generation == original.generation
    assert worker._session_is_current()

    connection.attach_ssh(FakeResource(), host="replacement.example")
    assert not worker._session_is_current()

def test_kubernetes_log_stream_worker_is_bound_to_shared_connection_generation():
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    services = AppServices(connection)
    original = connection.attach_ssh(FakeResource(), host="cluster-jump.example")

    worker = services.kubernetes.create_log_stream_worker("kubectl logs -f api-0")
    assert worker.connection_generation == original.generation
    assert worker._session_is_current()

    connection.attach_ssh(FakeResource(), host="replacement.example")
    assert not worker._session_is_current()



def test_domain_command_services_share_one_generation_guarded_worker_factory():
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    services = AppServices(connection)
    connection.attach_ssh(FakeResource(), host="shared.example")
    pool = []

    workers = [
        services.kubernetes.create_worker(pool, "kubectl get pods", timeout=11),
        services.docker.create_worker(pool, "docker ps", sudo_user="ops", timeout=12),
        services.dashboard.create_worker(pool, "uname -a", timeout=13, raw=True),
        services.terminal.create_command_worker(
            pool, "pwd", cwd="/srv/app", sudo_user="ops", timeout=14,
        ),
        services.files.create_command_worker(pool, "ls -la", timeout=15),
    ]

    assert pool == workers
    assert len({worker.connection_generation for worker in workers}) == 1
    assert len({worker.connection_session_id for worker in workers}) == 1
    assert all(worker._session_is_current() for worker in workers)

    assert workers[0].cmd == "kubectl get pods"
    assert workers[0].timeout == 11
    assert workers[1].sudo_user == "ops"
    assert workers[1].timeout == 12
    assert workers[2].raw is True
    assert workers[2].timeout == 13
    assert workers[3].cwd == "/srv/app"
    assert workers[3].sudo_user == "ops"
    assert workers[3].timeout == 14
    assert workers[4].cmd == "ls -la"
    assert workers[4].timeout == 15

    connection.disconnect()
    assert all(not worker._session_is_current() for worker in workers)



def test_sftp_transfer_worker_rejects_replaced_session_before_io():
    from workers_parts.transfer import _TransferWorker

    connection = ConnectionService()
    filesystem = FakeFilesystem()
    original = connection.attach_file_connection(
        filesystem, protocol="ftp", host="first.example",
    )
    worker = _TransferWorker(filesystem, "download", "/tmp/out", "/remote/file")
    worker._session_guard = lambda: connection.is_current(original.generation)
    errors = []
    worker.finished_err.connect(errors.append)

    connection.attach_file_connection(
        FakeFilesystem(), protocol="ftp", host="second.example",
    )
    worker.run()

    assert errors and "connection changed" in errors[0].lower()
    assert filesystem.calls == []


def test_scp_transfer_worker_does_not_start_after_session_replacement(monkeypatch):
    import pytest
    import workers_parts.transfer as transfer_parts

    connection = ConnectionService()
    original = connection.attach_ssh(FakeResource(), host="first.example")
    worker = transfer_parts.ScpTransferWorker(
        "first.example", 22, "operator", "/tmp/operator.pem", None,
        "upload", "/tmp/local", "/remote/file",
    )
    worker._session_guard = lambda: connection.is_current(original.generation)
    errors = []
    worker.finished_err.connect(errors.append)

    connection.attach_ssh(FakeResource(), host="second.example")
    monkeypatch.setattr(
        transfer_parts.os, "forkpty",
        lambda: pytest.fail("stale SCP worker must not start a subprocess"),
    )
    worker.run()

    assert errors and "connection changed" in errors[0].lower()



def test_app_services_shutdown_stops_and_joins_registered_workers():
    from PyQt5.QtCore import QThread

    class StoppableWorker(QThread):
        def __init__(self):
            super().__init__()
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

        def run(self):
            while not self.cancelled:
                self.msleep(5)

    connection = ConnectionService(pool_closer=lambda _ssh: None)
    services = AppServices(connection)
    workers = [StoppableWorker(), StoppableWorker(), StoppableWorker()]
    for worker in workers:
        services.files.register_worker(worker)
        worker.start()

    services.shutdown()

    assert all(worker.cancelled for worker in workers)
    assert all(not worker.isRunning() for worker in workers)
    assert services.commands._workers == []



def test_connection_handshake_workers_close_partial_resources_when_cancelled():
    from workers_parts.connection import ConnectWorker, FTPConnectionWorker

    ssh_worker = ConnectWorker("host", 22, "operator", "", "")
    ssh = FakeResource()
    sftp = FakeResource()
    ssh_worker._ssh_client = ssh
    ssh_worker._sftp_client = sftp
    ssh_worker.cancel()
    assert ssh_worker._cancelled.is_set()
    assert sftp.close_calls == 1
    assert ssh.close_calls == 1

    ftp_worker = FTPConnectionWorker(
        "files.example", 21, "operator", "secret",
    )
    filesystem = FakeResource()
    ftp_worker._filesystem = filesystem
    ftp_worker.cancel()
    assert ftp_worker._cancelled.is_set()
    assert filesystem.close_calls == 1



def test_connection_service_binds_workers_to_an_immutable_session_snapshot():
    from types import SimpleNamespace

    connection = ConnectionService()
    first = connection.attach_ssh(FakeResource(), host="first.example")
    worker = SimpleNamespace()
    assert connection.bind_worker(worker, first) is worker
    assert worker.connection_generation == first.generation
    assert worker.connection_session_id == first.session_id
    assert worker._session_guard()

    connection.attach_ssh(FakeResource(), host="second.example")
    assert not worker._session_guard()



def test_kubernetes_service_owns_context_flag_application():
    connection = ConnectionService()
    services = AppServices(connection)
    k8s = services.kubernetes

    assert k8s.context_flag("") == ""
    assert k8s.context_flag("prod") == "--context=prod"
    assert k8s.apply_context("  kubectl get pods", "prod") == (
        "  kubectl --context=prod get pods"
    )
    assert k8s.apply_context("kubectl --context=dev get pods", "prod") == (
        "kubectl --context=dev get pods"
    )
    assert k8s.apply_context("docker ps", "prod") == "docker ps"


def test_dashboard_service_pins_each_kubectl_line_to_selected_context():
    connection = ConnectionService()
    services = AppServices(connection)
    command = "kubectl get nodes\n  kubectl get pods -A\n echo done"

    assert services.dashboard.contextual_command(command, "prod") == (
        "kubectl --context=prod get nodes\n"
        "  kubectl --context=prod get pods -A\n"
        " echo done"
    )
    assert services.dashboard.contextual_command(command, "") == command
    explicit = "kubectl --context=staging get pods\\nkubectl get nodes"
    assert services.dashboard.contextual_command(explicit, "prod") == (
        "kubectl --context=staging get pods\\n"
        "kubectl --context=prod get nodes"
    )



def test_stream_workers_are_cancelled_on_reconnect_before_starting():
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    services = AppServices(connection)
    connection.attach_ssh(FakeResource(), host="first.example")

    workers = [
        services.docker.create_log_stream_worker("docker logs -f api"),
        services.kubernetes.create_log_stream_worker("kubectl logs -f api"),
        services.kubernetes.create_pod_exec_stream_worker(
            "default", "api-0", "api", "dev-context",
        ),
        services.files.create_exec_stream_worker("python3 /tmp/task.py"),
    ]

    connection.attach_ssh(FakeResource(), host="replacement.example")

    assert workers[0]._stop is True
    assert workers[1]._stop is True
    assert workers[2]._stop.is_set()
    assert workers[3]._stop is True
    assert all(not worker._session_is_current() for worker in workers)



def test_media_start_worker_rejects_a_replaced_session_before_starting():
    from workers_parts.media import _StreamServerStartWorker

    class FakeMediaServer:
        def __init__(self):
            self.start_calls = 0
            self.stop_calls = 0

        def start(self):
            self.start_calls += 1
            return "http://127.0.0.1:8000/media"

        def stop(self):
            self.stop_calls += 1

    connection = ConnectionService()
    services = AppServices(connection)
    snapshot = connection.attach_ssh(FakeResource(), host="first.example")
    server = FakeMediaServer()
    worker = _StreamServerStartWorker(server)
    connection.bind_worker(worker, snapshot)
    errors = []
    worker.error.connect(errors.append)

    connection.attach_ssh(FakeResource(), host="second.example")
    worker.run()

    assert server.start_calls == 0
    assert errors and "connection changed" in errors[0].lower()


def test_media_start_worker_stops_server_if_session_changes_during_start():
    from workers_parts.media import _StreamServerStartWorker

    connection = ConnectionService()
    services = AppServices(connection)
    snapshot = connection.attach_ssh(FakeResource(), host="first.example")

    class ReconnectingMediaServer:
        def __init__(self):
            self.stop_calls = 0

        def start(self):
            connection.attach_ssh(FakeResource(), host="second.example")
            return "http://127.0.0.1:8000/media"

        def stop(self):
            self.stop_calls += 1

    server = ReconnectingMediaServer()
    worker = _StreamServerStartWorker(server)
    connection.bind_worker(worker, snapshot)
    errors = []
    worker.error.connect(errors.append)

    worker.run()

    assert server.stop_calls == 1
    assert errors and "connection changed" in errors[0].lower()



def test_ftp_upload_stream_uses_a_dedicated_data_connection_and_records_bytes():
    import threading
    from ftp_fs import FTPFS

    class FakeDataConnection:
        def __init__(self):
            self.payload = bytearray()
            self.closed = False
            self.timeout = None

        def settimeout(self, value):
            self.timeout = value

        def sendall(self, data):
            self.payload.extend(data)

        def close(self):
            self.closed = True

    class FakeFTPConnection:
        def __init__(self):
            self.data = FakeDataConnection()
            self.commands = []
            self.voidresp_calls = 0
            self.quit_calls = 0
            self.close_calls = 0

        def voidcmd(self, command):
            self.commands.append(command)

        def transfercmd(self, command):
            self.commands.append(command)
            return self.data

        def voidresp(self):
            self.voidresp_calls += 1

        def quit(self):
            self.quit_calls += 1

        def close(self):
            self.close_calls += 1

    fs = FTPFS.__new__(FTPFS)
    fs._ftp = FakeFTPConnection()  # shared browsing control connection
    fs._new_connection = lambda: transfer_ftp
    fs._stats_lock = threading.Lock()
    fs.uploaded_bytes = 0
    fs.upload_count = 0
    fs.downloaded_bytes = 0
    fs.download_count = 0
    fs.last_operation = None
    fs.last_operation_at = None
    transfer_ftp = FakeFTPConnection()

    writer = fs.open_upload_stream("/remote/file.bin")
    writer.write(b"hello")
    writer.write(b" world")
    writer.close()

    assert transfer_ftp.commands == ["TYPE I", "STOR /remote/file.bin"]
    assert bytes(transfer_ftp.data.payload) == b"hello world"
    assert transfer_ftp.data.timeout == 60
    assert transfer_ftp.data.closed
    assert transfer_ftp.voidresp_calls == 1
    assert transfer_ftp.quit_calls == 1
    assert fs.uploaded_bytes == 11
    assert fs.upload_count == 1
    assert fs._ftp.close_calls == 0  # browsing session is untouched


def test_ftp_upload_stream_abort_does_not_finalize_or_count_partial_transfer():
    import threading
    from ftp_fs import FTPFS

    class FakeDataConnection:
        def __init__(self):
            self.payload = bytearray()
            self.closed = False

        def settimeout(self, _value):
            pass

        def sendall(self, data):
            self.payload.extend(data)

        def close(self):
            self.closed = True

    class FakeFTPConnection:
        def __init__(self):
            self.data = FakeDataConnection()
            self.voidresp_calls = 0
            self.quit_calls = 0
            self.close_calls = 0

        def voidcmd(self, _command):
            pass

        def transfercmd(self, _command):
            return self.data

        def voidresp(self):
            self.voidresp_calls += 1

        def quit(self):
            self.quit_calls += 1

        def close(self):
            self.close_calls += 1

    fs = FTPFS.__new__(FTPFS)
    fs._ftp = FakeFTPConnection()
    transfer_ftp = FakeFTPConnection()
    fs._new_connection = lambda: transfer_ftp
    fs._stats_lock = threading.Lock()
    fs.uploaded_bytes = 0
    fs.upload_count = 0
    fs.downloaded_bytes = 0
    fs.download_count = 0
    fs.last_operation = None
    fs.last_operation_at = None

    writer = fs.open_upload_stream("/remote/partial.bin")
    writer.write(b"partial")
    writer.abort()

    assert transfer_ftp.data.closed
    assert transfer_ftp.voidresp_calls == 0
    assert transfer_ftp.quit_calls == 0
    assert transfer_ftp.close_calls == 1
    assert fs.uploaded_bytes == 0
    assert fs.upload_count == 0



def test_ftp_download_worker_uses_a_dedicated_stream_not_the_shared_control_channel(tmp_path):
    from workers_parts.transfer import _TransferWorker

    class FakeReader:
        def __init__(self):
            self.chunks = [b"first-", b"second", b""]
            self.closed = False

        def read(self, _size):
            return self.chunks.pop(0)

        def close(self):
            self.closed = True

    class SharedControl:
        def retrbinary(self, *_args, **_kwargs):
            raise AssertionError("transfer must not use the shared FTP control channel")

    class FakeFTPFilesystem:
        _ftp = SharedControl()

        def __init__(self):
            self.reader = FakeReader()
            self.probe_calls = 0
            self.stream_calls = 0

        def stream_probe(self, _path):
            self.probe_calls += 1
            return 12, True

        def open_stream(self, _path):
            self.stream_calls += 1
            return self.reader

    filesystem = FakeFTPFilesystem()
    destination = tmp_path / "download.bin"
    worker = _TransferWorker(
        filesystem, "download", str(destination), "/remote/file.bin",
    )
    completed = []
    worker.finished_ok.connect(lambda: completed.append(True))

    worker.run()

    assert completed == [True]
    assert filesystem.probe_calls == 1
    assert filesystem.stream_calls == 1
    assert filesystem.reader.closed
    assert destination.read_bytes() == b"first-second"



def test_connection_service_cancels_bound_workers_on_reconnect_and_disconnect():
    from PyQt5.QtCore import QThread

    class BoundWorker(QThread):
        def __init__(self):
            super().__init__()
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    connection = ConnectionService()
    first = connection.attach_ssh(FakeResource(), host="first.example")
    first_worker = BoundWorker()
    connection.bind_worker(first_worker, first)

    second = connection.attach_ssh(FakeResource(), host="second.example")
    assert first_worker.cancelled

    second_worker = BoundWorker()
    connection.bind_worker(second_worker, second)
    connection.disconnect()
    assert second_worker.cancelled



def test_reconnect_cancels_an_active_transfer_handle():
    from workers_parts.transfer import _TransferWorker

    class ActiveHandle:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    connection = ConnectionService()
    snapshot = connection.attach_ssh(FakeResource(), host="first.example")
    worker = _TransferWorker(object(), "download", "/tmp/out", "/remote/file")
    connection.bind_worker(worker, snapshot)
    handle = ActiveHandle()
    worker._handle = handle

    connection.attach_ssh(FakeResource(), host="second.example")

    assert worker._cancelled
    assert handle.closed



def test_connection_health_worker_is_registered_for_session_replacement(monkeypatch):
    import workers

    class FakeSignal:
        def connect(self, callback):
            self.callback = callback

    class FakeHealthWorker:
        def __init__(self, ssh):
            self.ssh = ssh
            self.finished = FakeSignal()
            self.stopped = False

        def stop(self):
            self.stopped = True

    monkeypatch.setattr(workers, "ConnectionHealthWorker", FakeHealthWorker)
    connection = ConnectionService(pool_closer=lambda _ssh: None)
    connection.attach_ssh(FakeResource(), host="first.example")

    health_worker = connection.create_health_worker()
    assert health_worker.connection_generation == 1
    assert health_worker._session_guard()

    connection.attach_ssh(FakeResource(), host="second.example")

    assert health_worker.stopped
    assert not health_worker._session_guard()



def test_ftp_stream_reader_propagates_timeout_instead_of_reporting_eof():
    import socket
    from ftp_fs import _FTPStreamReader

    class TimedOutSocket:
        def recv(self, _size):
            raise socket.timeout("transfer stalled")

        def close(self):
            pass

    class FakeFTP:
        def close(self):
            pass

    reader = _FTPStreamReader(FakeFTP(), TimedOutSocket())
    import pytest
    with pytest.raises(socket.timeout, match="transfer stalled"):
        reader.read(4096)

    reader.close()
    assert reader.read(4096) == b""



def test_exec_stream_worker_is_owned_by_worker_layer_and_stop_closes_channel():
    from workers import ExecStreamWorker

    class FakeChannel:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    worker = ExecStreamWorker(FakeResource(), "tail -f /var/log/app.log")
    channel = FakeChannel()
    worker._channel = channel

    worker.request_stop()

    assert worker._stop
    assert channel.close_calls == 1



def test_domain_services_and_shared_exec_worker_do_not_import_dialog_modules():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    modules = [
        root / "services" / "kubernetes.py",
        root / "services" / "docker.py",
        root / "services" / "files.py",
        root / "services" / "dashboard.py",
        root / "services" / "terminal.py",
        root / "services" / "ai.py",
        root / "services" / "webapp.py",
        root / "workers_parts" / "exec_stream.py",
    ]

    for module in modules:
        source = module.read_text(encoding="utf-8")
        assert "dialogs_parts" not in source, module
        assert "from dialogs " not in source, module
