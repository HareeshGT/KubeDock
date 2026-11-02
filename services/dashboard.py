"""Dashboard telemetry collection backed by the shared command service."""

from __future__ import annotations


class DashboardService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

    @property
    def ssh_client(self):
        return self.connection.get_ssh_client()

    def create_worker(self, worker_pool, command, *, timeout=None, raw=False):
        return self.commands.create_worker(
            worker_pool, command, timeout=timeout, raw=raw,
        )
