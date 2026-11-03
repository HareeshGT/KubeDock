"""Remote file operations backed by the shared session."""

from __future__ import annotations

from .lifecycle import activate_lifecycle


class FileManagerService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands
    def stop(self):
        """Cancel background work owned by this feature service."""
        if self.commands is not None:
            self.commands.stop_service("files")


    @property
    def filesystem(self):
        return self.connection.get_filesystem()

    @property
    def has_ssh(self):
        snapshot = self.connection.snapshot
        return snapshot.is_active and snapshot.protocol == "ssh"

    def create_directory_worker(self, worker_pool, path):
        activate_lifecycle(self)
        filesystem, snapshot = self.connection.get_filesystem_session()
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        from workers import DirectoryListWorker, track_worker

        worker = DirectoryListWorker(filesystem, path)
        self.connection.bind_worker(worker, snapshot)
        self.register_worker(worker)
        if worker_pool is not None:
            track_worker(worker_pool, worker)
        return worker

    def create_stream_reader(self, path, *, sudo_user=None, max_bytes=None):
        activate_lifecycle(self)
        filesystem, ssh, snapshot = self.connection.get_file_session()
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        from workers import FileStreamReadWorker

        worker = FileStreamReadWorker(
            filesystem, ssh, path,
            sudo_user=sudo_user, max_bytes=max_bytes,
        )
        self.connection.bind_worker(worker, snapshot)
        return self.register_worker(worker, diagnostic_key="files:read")

    def capture_transfer_session(self):
        activate_lifecycle(self)
        """Capture the active filesystem adapter and generation for a transfer."""
        filesystem, snapshot = self.connection.get_filesystem_session()
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        return filesystem, snapshot

    def bind_worker(self, worker, snapshot):
        """Bind and register a file worker against its captured session."""
        self.connection.bind_worker(worker, snapshot)
        return self.register_worker(worker)

    def is_current(self, generation):
        return self.connection.is_current(generation)

    def register_worker(self, worker, diagnostic_key="files:worker"):
        """Register a file-related worker with shutdown and diagnostics ownership."""
        return self.commands.register_worker(
            worker, diagnostic_key=diagnostic_key,
        )

    def create_command_worker(self, worker_pool, command, *, sudo_user=None, timeout=None, raw=False):
        activate_lifecycle(self)
        return self.commands.create_worker(
            worker_pool, command, sudo_user=sudo_user, timeout=timeout, raw=raw,
            diagnostic_key="files:command",
        )

    def normalize(self, path):
        activate_lifecycle(self)
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        return filesystem.normalize(path or "")

    def stat(self, path):
        activate_lifecycle(self)
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        return filesystem.stat(path)

    def mkdir(self, path):
        activate_lifecycle(self)
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        return filesystem.mkdir(path)

    def write_bytes(self, path, data):
        """Write bytes through the active SSH/SFTP or FTP filesystem adapter."""
        activate_lifecycle(self)
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        payload = bytes(data)
        if hasattr(filesystem, "write_bytes"):
            return filesystem.write_bytes(path, payload)
        if hasattr(filesystem, "_ftp"):
            import io
            return filesystem.putfo(io.BytesIO(payload), path)
        import io
        return filesystem._sftp.putfo(io.BytesIO(payload), path)

    def create_empty_file(self, path):
        activate_lifecycle(self)
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        if hasattr(filesystem, "write_bytes"):
            return filesystem.write_bytes(path, b"")
        handle = filesystem.open(path, "w")
        handle.close()

    def delete(self, path, *, is_directory=False):
        activate_lifecycle(self)
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        if is_directory:
            return filesystem.rmdir(path)
        return filesystem.remove(path)

    def rename(self, old_path, new_path):
        activate_lifecycle(self)
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        return filesystem.rename(old_path, new_path)
    def create_media_stream_start_worker(self, path, *, sudo_user=None):
        """Create a media server and its generation-bound startup worker."""
        activate_lifecycle(self)
        filesystem, ssh, snapshot = self.connection.get_file_session()
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system before streaming media")
        if ssh is None and not hasattr(filesystem, "_ftp"):
            raise RuntimeError("The active file session cannot stream this media source")
        from workers import MediaStreamServer, _StreamServerStartWorker

        server = MediaStreamServer(ssh, filesystem, path, sudo_user=sudo_user)
        worker = _StreamServerStartWorker(server)
        self.connection.bind_worker(worker, snapshot)
        self.register_worker(worker)
        return server, worker, snapshot

    def create_exec_stream_worker(self, command):
        """Create a streaming remote-file execution worker on the active SSH session."""
        activate_lifecycle(self)
        ssh, snapshot = self.connection.get_ssh_session()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before executing a remote file")
        from workers import ExecStreamWorker

        worker = ExecStreamWorker(ssh, command)
        self.connection.bind_worker(worker, snapshot)
        return self.register_worker(worker, diagnostic_key="files:exec-stream")
