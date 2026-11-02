"""Docker domain operations backed by the shared command service."""

from __future__ import annotations


class DockerService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

    @property
    def ssh_client(self):
        return self.connection.get_ssh_client()

    def create_worker(self, worker_pool, command, *, sudo_user=None, timeout=None, raw=False):
        return self.commands.create_worker(
            worker_pool, command, sudo_user=sudo_user, timeout=timeout, raw=raw,
        )
