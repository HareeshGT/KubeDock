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
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        from workers import DirectoryListWorker, track_worker

        worker = DirectoryListWorker(filesystem, path)
        if worker_pool is not None:
            track_worker(worker_pool, worker)
        return worker

    def create_stream_reader(self, path, *, sudo_user=None, max_bytes=None):
        filesystem = self.filesystem
        if filesystem is None:
            raise RuntimeError("Connect to a remote file system first")
        from workers import FileStreamReadWorker

        return FileStreamReadWorker(
            filesystem, self.ssh_client, path,
            sudo_user=sudo_user, max_bytes=max_bytes,
        )

    def create_command_worker(self, worker_pool, command, *, sudo_user=None, timeout=None, raw=False):
        return self.commands.create_worker(
            worker_pool, command, sudo_user=sudo_user, timeout=timeout, raw=raw,
        )
