"""Application-wide service composition root."""

from __future__ import annotations

from typing import Optional

from .connection import ConnectionService


class AppServices:
    """Owns the process-local services shared by KubeDock features.

    Feature tabs and adapters receive services from this object instead of
    creating their own connections. More domain services can be registered
    here as they are extracted from the UI modules.
    """

    def __init__(self, connection: Optional[ConnectionService] = None):
        self.connection = connection or ConnectionService()
        self._stopped = False

    def shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.connection.disconnect()

    @property
    def stopped(self) -> bool:
        return self._stopped
