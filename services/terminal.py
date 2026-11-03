"""Interactive and integrated terminal command service."""

from __future__ import annotations

from .lifecycle import activate_lifecycle


class TerminalService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

    def create_command_worker(self, worker_pool, command, *, cwd=None, sudo_user=None, timeout=None):
        activate_lifecycle(self)
        return self.commands.create_worker(
            worker_pool, command, cwd=cwd, sudo_user=sudo_user, timeout=timeout,
        )
