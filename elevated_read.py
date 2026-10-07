"""elevated_read.py — narrowly scoped ``sudo`` fallback for remote file downloads.

When a plain SFTP read fails because the *remote* login user may not read a
file, the download worker retries that one read as::

    sudo -n [-u <user>] -- cat -- '<path>'

Design rules (see tests/test_elevated_download.py):

* Only a remote *permission-denied* triggers the retry — never a timeout,
  dropped connection, missing file, full disk or corrupt transfer.
* ``sudo -n`` (non-interactive) is always used. The app has no sudo password
  store (the existing "switch user" flow also probes with ``sudo -n``), so
  nothing here ever sends, stores or logs a password. If sudo wants one, the
  user gets a clear error instead of a hang.
* Only ``cat`` of a single, shell-quoted path is elevated — never a
  user-supplied command string, and never anything on the local machine.
* Bytes are streamed untouched to the local file (no text decoding).
"""

import errno
import os
import shlex
import time
from typing import Callable, Optional

CHUNK_SIZE = 256 * 1024
_STDERR_LIMIT = 8192


def is_permission_denied(exc):
    # type: (BaseException) -> bool
    """True only for a remote *permission denied* (EACCES/EPERM).

    paramiko maps SFTP_PERMISSION_DENIED to ``IOError(errno.EACCES, ...)``,
    which Python surfaces as ``PermissionError``. Everything else (missing
    file, socket/SSH errors, timeouts, ENOSPC, ...) returns False so it is
    never retried with elevated privileges.
    """
    if isinstance(exc, FileNotFoundError):
        return False
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        if exc.errno in (errno.EACCES, errno.EPERM):
            return True
        if exc.errno is None and "permission denied" in str(exc).lower():
            return True
    return False


def build_elevated_read_command(path, sudo_user=None):
    # type: (str, Optional[str]) -> str
    """Build the one remote command used for an elevated read.

    ``shlex.quote`` makes spaces, quotes, ``$``, ``;``, ``&`` etc. inert;
    ``--`` after ``sudo`` and after ``cat`` stops a path such as ``-rf`` or a
    user name from being parsed as an option.
    """
    cmd = ["sudo", "-n"]
    if sudo_user:
        cmd += ["-u", sudo_user]
    cmd += ["--", "cat", "--", path]
    return " ".join(shlex.quote(part) for part in cmd)


class ElevatedReadError(Exception):
    """User-presentable failure of an elevated read."""


def classify_failure(stderr, exit_code, path):
    # type: (str, int, str) -> str
    """Turn sudo/cat stderr into a clear, user-facing message."""
    low = (stderr or "").lower()
    if ("a password is required" in low or "a terminal is required" in low
            or "no tty present" in low or "askpass" in low):
        return (
            "Permission denied reading '{}', and sudo on the remote host needs "
            "a password. Passwordless sudo (NOPASSWD) is required for "
            "automatic elevated downloads.".format(path)
        )
    if ("not in the sudoers" in low or "may not run sudo" in low
            or "is not allowed to execute" in low or "sorry, user" in low):
        return (
            "Permission denied reading '{}', and this remote user is not "
            "allowed to use sudo for it.".format(path)
        )
    if "sudo: command not found" in low or "sudo: not found" in low:
        return (
            "Permission denied reading '{}', and sudo is not installed on the "
            "remote host.".format(path)
        )
    detail = (stderr or "").strip()
    if len(detail) > 300:
        detail = detail[:300] + "…"
    return "Elevated read of '{}' failed (exit {}){}".format(
        path, exit_code, ": " + detail if detail else "")


def download_elevated(
    ssh,
    remote_path,                       # type: str
    local_path,                        # type: str
    sudo_user=None,                    # type: Optional[str]
    total=0,                           # type: int
    on_progress=None,                  # type: Optional[Callable[[int, int], None]]
    is_cancelled=None,                 # type: Optional[Callable[[], bool]]
    open_session=None,
    close_session=None,
    chunk_size=CHUNK_SIZE,
):
    # type: (...) -> bool
    """Stream ``sudo -n cat <remote_path>`` into *local_path*.

    Returns True on success, False if cancelled. Raises
    :class:`ElevatedReadError` (clear message) on any remote failure. The local
    file is created only once data arrives (or on a clean, empty success) and a
    partial file is removed on failure/cancel, so a failed attempt leaves
    nothing behind.
    """
    if open_session is None or close_session is None:
        from workers_parts.ssh import open_managed_session, close_managed_session
        open_session = open_session or open_managed_session
        close_session = close_session or close_managed_session

    cancelled = is_cancelled or (lambda: False)
    cmd = build_elevated_read_command(remote_path, sudo_user)

    channel = open_session(ssh)
    out = None
    done = 0
    stderr_buf = bytearray()
    ok = False
    try:
        channel.exec_command(cmd)
        while True:
            if cancelled():
                return False
            progressed = False
            if channel.recv_stderr_ready():
                chunk = channel.recv_stderr(4096)
                if chunk:
                    progressed = True
                    stderr_buf += chunk
                    del stderr_buf[:-_STDERR_LIMIT]
            if channel.recv_ready():
                buf = channel.recv(chunk_size)
                if buf:
                    progressed = True
                    if out is None:
                        out = open(local_path, "wb")
                    out.write(buf)
                    done += len(buf)
                    if on_progress:
                        on_progress(done, total or done)
            if progressed:
                continue
            if (channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()):
                break
            time.sleep(0.02)

        code = channel.recv_exit_status()
        if code != 0:
            raise ElevatedReadError(
                classify_failure(stderr_buf.decode(errors="replace"), code, remote_path))
        if out is None:                       # genuinely empty remote file
            out = open(local_path, "wb")
        if on_progress:
            on_progress(done, total or done)
        ok = True
        return True
    finally:
        if out is not None:
            try:
                out.close()
            except Exception:
                pass
            if not ok:
                try:
                    os.remove(local_path)
                except OSError:
                    pass
        close_session(channel)