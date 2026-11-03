"""Application-wide service composition root."""

from __future__ import annotations

import threading
from typing import Optional

from .connection import ConnectionService
from .command import CommandService
from .kubernetes import KubernetesService
from .docker import DockerService
from .dashboard import DashboardService
from .files import FileManagerService
from .terminal import TerminalService
from .ai import AIService
from .webapp import WebAppService
from .lifecycle import ServiceLifecycleManager


class AppServices:
    """Own process-local services and their shared infrastructure.

    Registration is lazy: creating the desktop runtime does not start feature
    polling, investigations, or the optional HTTP server.
    """

    def __init__(self, connection: Optional[ConnectionService] = None):
        self.connection = connection or ConnectionService()
        self.commands = CommandService(self.connection)
        self.kubernetes = KubernetesService(self.connection, self.commands)
        self.docker = DockerService(self.connection, self.commands)
        self.dashboard = DashboardService(self.connection, self.commands)
        self.files = FileManagerService(self.connection, self.commands)
        self.terminal = TerminalService(self.connection, self.commands)
        self.ai = AIService(self.commands)
        self.webapp = WebAppService(self.connection, self.commands)
        self.lifecycle = ServiceLifecycleManager()
        self.lifecycle.register("connection", self.connection)
        self.lifecycle.register("commands", self.commands, dependencies=("connection",))
        self.lifecycle.register("kubernetes", self.kubernetes, dependencies=("commands",))
        self.lifecycle.register("docker", self.docker, dependencies=("commands",))
        self.lifecycle.register("monitoring", self.dashboard, dependencies=("commands",))
        self.lifecycle.register("files", self.files, dependencies=("connection",))
        self.lifecycle.register("terminal", self.terminal, dependencies=("commands",))
        self.lifecycle.register("ai", self.ai)
        self.lifecycle.register("webapp", self.webapp, dependencies=("connection", "commands"))
        self._lock = threading.Lock()
        self._stopped = False

    def service_status(self):
        """Return credential-free diagnostics for all registered services."""
        statuses = self.lifecycle.statuses()
        worker_count = self.commands.active_worker_count()
        operation_count = self.commands.active_operation_count()
        for name in statuses:
            self.lifecycle.set_activity(
                name,
                worker_count=worker_count if name in ("commands", "connection") else 0,
                active_operations=operation_count if name == "commands" else 0,
            )
        return self.lifecycle.statuses()

    def shutdown(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        # Stop feature-owned activity first, then workers, then shared transport.
        try:
            self.lifecycle.shutdown()
        finally:
            self.commands.request_worker_shutdown()
            try:
                self.connection.disconnect()
            finally:
                self.commands.wait_for_workers(timeout_seconds=3.0)

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped
