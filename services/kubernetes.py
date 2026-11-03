"""Kubernetes domain operations backed by the shared command service."""

from __future__ import annotations

import re
import shlex


class KubernetesService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

    @property
    def ssh_client(self):
        return self.connection.get_ssh_client()

    @staticmethod
    def context_flag(context):
        value = str(context or "").strip()
        return f"--context={shlex.quote(value)}" if value else ""

    @classmethod
    def apply_context(cls, command, context):
        """Apply the selected Kubernetes context without duplicating flags."""
        if not command or not str(context or "").strip():
            return command
        stripped = command.lstrip()
        if not re.match(r"^kubectl(?:\\s|$)", stripped):
            return command
        if re.search(r"(?:^|\\s)--context(?:=|\\s)", stripped):
            return command
        prefix_len = len(command) - len(stripped)
        return (
            command[:prefix_len] + "kubectl " + cls.context_flag(context)
            + stripped[len("kubectl"):]
        )

    def create_worker(self, worker_pool, command, *, sudo_user=None, timeout=None, raw=False):
        """Run a prepared kubectl command without opening a feature connection."""
        return self.commands.create_worker(
            worker_pool, command, sudo_user=sudo_user, timeout=timeout, raw=raw,
        )
    def create_pod_exec_stream_worker(
        self, namespace, pod, container=None, context=None, *, cols=120, rows=32,
    ):
        """Create a persistent pod shell worker bound to the active SSH generation."""
        ssh, snapshot = self.connection.get_ssh_session()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before opening a pod shell")
        from workers import PodExecStreamWorker

        worker = PodExecStreamWorker(
            ssh, namespace, pod, container, context, cols=cols, rows=rows,
        )
        self.connection.bind_worker(worker, snapshot)
        return self.commands.register_worker(worker)
    def create_log_stream_worker(self, command):
        """Create a live Kubernetes log stream guarded by the shared session generation."""
        ssh, snapshot = self.connection.get_ssh_session()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before streaming Kubernetes logs")
        from dialogs_parts.file_exec import _ExecStreamWorker

        worker = _ExecStreamWorker(ssh, command)
        self.connection.bind_worker(worker, snapshot)
        return self.commands.register_worker(worker)
