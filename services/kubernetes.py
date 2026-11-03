"""Kubernetes domain operations backed by the shared command service."""

from __future__ import annotations

import re
import shlex


class KubernetesService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

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
        if not re.match(r"^kubectl(?:\s|$)", stripped):
            return command
        if re.search(r"(?:^|\s)--context(?:=|\s)", stripped):
            return command
        prefix_len = len(command) - len(stripped)
        return (
            command[:prefix_len] + "kubectl " + cls.context_flag(context)
            + stripped[len("kubectl"):]
        )

    @staticmethod
    def _remote_path_expression(path):
        value = str(path or "").strip()
        if not value:
            raise ValueError("A remote tunnel CSV path is required")
        if value.startswith("~"):
            return '"$HOME"' + shlex.quote(value[1:])
        return shlex.quote(value)

    def load_tunnel_services(self, csv_path):
        """Read and validate tunnel definitions through the shared command service."""
        import csv
        command = "cat {} 2>/dev/null".format(
            self._remote_path_expression(csv_path)
        )
        try:
            code, output, _error = self.commands.execute(command, timeout=10)
        except Exception:
            return []
        if code != 0 or not output.strip():
            return []
        services = []
        for index, row in enumerate(csv.reader(output.splitlines())):
            row = [cell.strip() for cell in row]
            if len(row) < 3 or not row[0]:
                continue
            name, local_port, container_port = row[:3]
            if index == 0 and not local_port.isdigit():
                continue
            if not local_port.isdigit() or not container_port.isdigit():
                continue
            services.append({
                "name": name,
                "port": int(local_port),
                "namespace": row[3] if len(row) > 3 else "",
                "container_port": int(container_port),
            })
        return services

    def create_tunnel_save_worker(self, worker_pool, csv_path, csv_text):
        """Create a managed worker that safely writes tunnel definitions to the VM."""
        import base64
        encoded = base64.b64encode(
            str(csv_text).encode("utf-8")
        ).decode("ascii")
        path_expr = self._remote_path_expression(csv_path)
        command = (
            "target={path}; "
            'mkdir -p "$(dirname "$target")" && '
            "printf '%s' {payload} | base64 -d > \"$target\""
        ).format(path=path_expr, payload=shlex.quote(encoded))
        return self.create_worker(worker_pool, command, timeout=20)

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
        from workers import ExecStreamWorker

        worker = ExecStreamWorker(ssh, command)
        self.connection.bind_worker(worker, snapshot)
        return self.commands.register_worker(worker)
