"""Pure helpers for constructing remote shell commands.

Keeping shell assembly outside the Qt worker makes it testable without a
running Qt event loop and prevents avoidable SSH round-trips for environment
setup.
"""

import shlex
from typing import Optional


def build_prepared_command(command: str, cwd: Optional[str] = None,
                           sudo_user: Optional[str] = None) -> str:
    """Prepare a POSIX remote command while preserving its execution user.

    `$HOME` is expanded by the remote shell that runs the command. This is
    equivalent to probing it in a separate SSH channel first, but uses one
    channel per command instead of two. For sudo commands the expansion occurs
    inside the target user's shell.
    """
    prefix = "cd {} 2>/dev/null; ".format(shlex.quote(cwd)) if cwd else ""
    inner = (
        'export PATH="$HOME:$HOME/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH";'
        + prefix + command
    )
    if sudo_user:
        safe_inner = inner.replace("'", "'\\''")
        return "sudo -n -u {} sh -c '{}'".format(shlex.quote(sudo_user), safe_inner)
    return inner


def command_operation_label(command: str) -> str:
    """Return a non-sensitive operation label for diagnostics.

    Resource names, arguments, command output, and credentials are deliberately
    excluded from this label.
    """
    import re

    for line in (command or "").splitlines():
        match = re.search(
            r"\bkubectl\s+(?:(?:--context(?:=|\s+)\S+)\s+)?([a-z][\w-]*)"
            r"(?:\s+([a-z][\w,-]*))?",
            line,
        )
        if match:
            action, resource = match.groups()
            if action == "config" and resource == "get-contexts":
                return "kubectl config get-contexts"
            return "kubectl {}{}".format(
                action, " " + resource if resource and not resource.startswith("--") else ""
            )
    return "remote shell command"
