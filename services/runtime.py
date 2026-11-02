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


class AppServices:
    """Owns the process-local services shared by KubeDock features.

    Feature tabs and adapters receive services from this object instead of
    creating their own connections. All current domain services share the
    same ConnectionService and CommandService.
    """

    def __init__(self, connection: Optional[ConnectionService] = None):
        self.connection = connection or ConnectionService()
        # One shared command executor backs every remote feature service.
        self.commands = CommandService(self.connection)
        self.kubernetes = KubernetesService(self.connection, self.commands)
        self.docker = DockerService(self.connection, self.commands)
        self.dashboard = DashboardService(self.connection, self.commands)
        self.files = FileManagerService(self.connection, self.commands)
        self.terminal = TerminalService(self.connection, self.commands)
        self.ai = AIService()
        self.webapp = WebAppService(self.connection, self.commands)
        self._lock = threading.Lock()
        self._stopped = False

    def shutdown(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        self.connection.disconnect()

    @property
    def stopped(self) -> bool:
        with self._lock:
            return self._stopped
