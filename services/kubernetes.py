"""Kubernetes domain operations backed by the shared command service."""

from __future__ import annotations


class KubernetesService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

    @property
    def ssh_client(self):
        return self.connection.get_ssh_client()

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
        worker.connection_generation = snapshot.generation
        worker.connection_session_id = snapshot.session_id
        worker._session_guard = lambda generation=snapshot.generation: self.connection.is_current(generation)
        return worker
    def create_log_stream_worker(self, command):
        """Create a live Kubernetes log stream guarded by the shared session generation."""
        ssh, snapshot = self.connection.get_ssh_session()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before streaming Kubernetes logs")
        from dialogs_parts.file_exec import _ExecStreamWorker

        worker = _ExecStreamWorker(ssh, command)
        worker.connection_generation = snapshot.generation
        worker.connection_session_id = snapshot.session_id
        worker._session_guard = lambda generation=snapshot.generation: self.connection.is_current(generation)
        return worker
