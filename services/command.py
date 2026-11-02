"""Shared remote command execution built on KubeDock's bounded SSH pool."""

from __future__ import annotations

from typing import Optional


class CommandService:
    """Create managed command workers for every feature using one SSH session.

    The service is intentionally the only place in the app service layer that
    constructs generic SSH command workers. Worker execution remains on Qt's
    QThread model and every command continues to use managed_exec_command.
    """

    def __init__(self, connection):
        self.connection = connection

    def create_worker(
        self,
        worker_pool: Optional[list],
        command: str,
        *,
        cwd: Optional[str] = None,
        sudo_user: Optional[str] = None,
        timeout: Optional[int] = None,
        raw: bool = False,
    ):
        ssh = self.connection.get_ssh_client()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before running remote commands")

        # Lazy imports avoid coupling service startup to Qt worker modules.
        from workers import CommandWorker, track_worker

        worker = CommandWorker(
            ssh, command, cwd=cwd, sudo_user=sudo_user,
            timeout=timeout, raw=raw,
        )
        if worker_pool is not None:
            track_worker(worker_pool, worker)
        return worker

    def execute(self, command: str, *, timeout: int = 30, input_data: bytes = None):
        """Execute a bounded command synchronously for non-UI service callers.

        UI features should use create_worker() so they never block the Qt
        event loop. This path is intended for backend/service adapters.
        """
        ssh = self.connection.get_ssh_client()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before running remote commands")
        from workers import managed_exec_command

        with managed_exec_command(ssh, command, channel_timeout=timeout) as (stdin, stdout, stderr):
            for stream in (stdout, stderr):
                stream.channel.settimeout(max(0.1, float(timeout)))
            if input_data is not None:
                stdin.write(input_data)
                stdin.flush()
                stdin.channel.shutdown_write()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
        return code, out, err
