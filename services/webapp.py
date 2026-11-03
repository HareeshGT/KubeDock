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

    def open_sftp(self):
        """Open a short-lived SFTP channel on the active shared SSH session."""
        ssh, snapshot = self.connection.get_ssh_session()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before opening SFTP")
        sftp = ssh.open_sftp()
        if not self.connection.is_current(snapshot.generation):
            try:
                sftp.close()
            except Exception:
                pass
            raise RuntimeError("The active connection changed while opening SFTP")
        return sftp

    def open_terminal_channel(self, *, term="xterm-256color", width=120, height=32):
        """Open an interactive PTY on the active session, never a new SSH client."""
        ssh, snapshot = self.connection.get_ssh_session()
        if ssh is None:
            raise RuntimeError("Connect to an SSH host before opening a terminal")
        channel = ssh.invoke_shell(term=term, width=width, height=height)
        if not self.connection.is_current(snapshot.generation):
            try:
                channel.close()
            except Exception:
                pass
            raise RuntimeError("The active connection changed while opening the terminal")
        return channel

    def execute(self, command, *, timeout=30, input_data=None, expected_ssh=None):
        return self.commands.execute(
            command, timeout=timeout, input_data=input_data,
            expected_ssh=expected_ssh,
        )
