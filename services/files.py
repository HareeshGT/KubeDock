"""Remote file operations backed by the shared session."""

from __future__ import annotations


class FileManagerService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

    @property
    def filesystem(self):
        return self.connection.get_filesystem()

    @property
    def ssh_client(self):
        return self.connection.get_ssh_client()

    def create_directory_worker(self, worker_pool, path):
        filesystem, snapshot = self.connection.get_filesystem_session()
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        from workers import DirectoryListWorker, track_worker

        worker = DirectoryListWorker(filesystem, path)
        self.connection.bind_worker(worker, snapshot)
        self.commands.register_worker(worker)
        if worker_pool is not None:
            track_worker(worker_pool, worker)
        return worker

    def create_stream_reader(self, path, *, sudo_user=None, max_bytes=None):
        filesystem, ssh, snapshot = self.connection.get_file_session()
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        from workers import FileStreamReadWorker

        worker = FileStreamReadWorker(
            filesystem, ssh, path,
            sudo_user=sudo_user, max_bytes=max_bytes,
        )
        self.connection.bind_worker(worker, snapshot)
        return self.commands.register_worker(worker)

    def register_worker(self, worker):
        """Register a file-related worker with application shutdown ownership."""
        return self.commands.register_worker(worker)

    def create_command_worker(self, worker_pool, command, *, sudo_user=None, timeout=None, raw=False):
        return self.commands.create_worker(
            worker_pool, command, sudo_user=sudo_user, timeout=timeout, raw=raw,
        )

    def normalize(self, path):
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        return filesystem.normalize(path or "")

    def stat(self, path):
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        return filesystem.stat(path)

    def mkdir(self, path):
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        return filesystem.mkdir(path)

    def write_bytes(self, path, data):
        """Write bytes through the active SSH/SFTP or FTP filesystem adapter."""
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
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        if hasattr(filesystem, "write_bytes"):
            return filesystem.write_bytes(path, b"")
        handle = filesystem.open(path, "w")
        handle.close()

    def delete(self, path, *, is_directory=False):
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        if is_directory:
            return filesystem.rmdir(path)
        return filesystem.remove(path)

    def rename(self, old_path, new_path):
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        return filesystem.rename(old_path, new_path)
    def create_exec_stream_worker(self, command):
        """Create a streaming remote-file execution worker on the active SSH session."""
        ssh, snapshot = self.connection.get_ssh_session()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before executing a remote file")
        from workers import ExecStreamWorker

        worker = ExecStreamWorker(ssh, command)
        self.connection.bind_worker(worker, snapshot)
        return self.commands.register_worker(worker)
