"""Restricted runtime adapter for the embedded Web App."""

from __future__ import annotations


class WebAppService:
    """Expose only the active session providers required by the web backend.

    The FastAPI server remains embedded in the desktop process. It does not
    own or create SSH connections and resolves the active client for each
    request so reconnects are reflected immediately.
    """

    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

    def ssh_provider(self):
        return self.connection.get_ssh_client()

    def filesystem_provider(self):
        return self.connection.get_filesystem()

    def execute(self, command, *, timeout=30, input_data=None, expected_ssh=None):
        return self.commands.execute(
            command, timeout=timeout, input_data=input_data,
            expected_ssh=expected_ssh,
        )
