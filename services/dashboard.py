"""Dashboard telemetry collection backed by the shared command service."""

from __future__ import annotations

import re
import shlex


class DashboardService:
    def __init__(self, connection, commands):
        self.connection = connection
        self.commands = commands

    def contextual_command(self, command, context):
        """Pin every top-level kubectl invocation to the selected context."""
        value = str(context or "").strip()
        if not value:
            return command
        flag = f"--context={shlex.quote(value)}"
        return re.sub(
            r"(^\s*)kubectl(?!\s+--context(?:=|\s))(?=\s)",
            lambda match: match.group(1) + "kubectl " + flag,
            command,
            flags=re.MULTILINE,
        )

    def create_worker(self, worker_pool, command, *, timeout=None, raw=False):
        return self.commands.create_worker(
            worker_pool, command, timeout=timeout, raw=raw,
        )
