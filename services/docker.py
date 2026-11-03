"""Docker domain operations backed by the shared command service."""

from __future__ import annotations


class DockerService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

    @property
    def ssh_client(self):
        return self.connection.get_ssh_client()

    def create_worker(self, worker_pool, command, *, sudo_user=None, timeout=None, raw=False):
        return self.commands.create_worker(
            worker_pool, command, sudo_user=sudo_user, timeout=timeout, raw=raw,
        )

    def create_log_stream_worker(self, command):
        """Create a live Docker log worker bound to the active session generation."""
        ssh, snapshot = self.connection.get_ssh_session()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before streaming Docker logs")
        from dialogs_parts.file_exec import _ExecStreamWorker

        worker = _ExecStreamWorker(ssh, command)
        worker.connection_generation = snapshot.generation
        worker.connection_session_id = snapshot.session_id
        worker._session_guard = lambda generation=snapshot.generation: self.connection.is_current(generation)
        return worker
