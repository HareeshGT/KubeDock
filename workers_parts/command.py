"""KubeDock workers split module.

This module is an internal implementation module. Public compatibility
symbols are re-exported by the top-level ``workers.py`` facade.
"""

import codecs
import os
import re
import signal
import mimetypes
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal

from .ssh import managed_exec_command, open_managed_session, close_managed_session

class CommandWorker(QThread):
    """Runs a single SSH command in a background thread."""

    done  = pyqtSignal(str)
    error = pyqtSignal(str)
    # Emitted alongside `done` (never instead of it) whenever the remote
    # command actually completes — the raw stdout, raw stderr, and the
    # real exit status of self.cmd, kept separate rather than merged into
    # one string. `done` keeps emitting exactly the same combined text it
    # always has, so every existing call site is unaffected; only callers
    # that need genuine pass/fail state (e.g. the terminal/ExecDialog
    # "Explain this error" AI feature) need to connect to this instead.
    result = pyqtSignal(str, str, int)

    # Safety net: exec_command's stdout only closes once the remote command
    # actually exits. A script that runs something long-lived in the
    # foreground (e.g. a blocking 'kubectl port-forward' without '&') never
    # exits, so without a timeout this would hang forever with no feedback
    # at all — no output, no error, no next prompt.
    DEFAULT_TIMEOUT = 45  # seconds

    def __init__(self, ssh, cmd, cwd=None, sudo_user=None, timeout=None, raw=False):
        # type: (object, str, Optional[str], Optional[str], Optional[int], bool) -> None
        super().__init__()
        self.ssh       = ssh
        self.cmd       = cmd
        self.cwd       = cwd
        self.sudo_user = sudo_user
        self.timeout   = timeout or self.DEFAULT_TIMEOUT
        # raw=True: send self.cmd exactly as given. Skips the POSIX-only
        # `echo $HOME` / `export PATH=...` preamble, which cmd.exe/PowerShell
        # (Windows SSH hosts) cannot run.
        self.raw       = raw
        self.finished.connect(self.deleteLater)

    def run(self):
        try:
            if self.raw:
                self._run_raw()
                return
            home_cmd = (
                "sudo -u {} sh -c 'echo $HOME'".format(self.sudo_user)
                if self.sudo_user else "echo $HOME"
            )
            with managed_exec_command(self.ssh, home_cmd) as (_stdin, stdout, _stderr):
                home = stdout.read().decode(errors="replace").strip()

            prefix = "cd {} 2>/dev/null; ".format(self.cwd) if self.cwd else ""
            inner = (
                "export PATH={h}:{h}/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH;".format(h=home)
                + prefix + self.cmd
            )
            if self.sudo_user:
                safe_inner = inner.replace("'", "'\\''")
                cmd = "sudo -u {} sh -c '{}'".format(self.sudo_user, safe_inner)
            else:
                cmd = inner

            with managed_exec_command(self.ssh, cmd) as (_stdin, stdout, stderr):
                stdout.channel.settimeout(self.timeout)
                try:
                    out = stdout.read().decode(errors="replace")
                    err = stderr.read().decode(errors="replace")
                except Exception as read_err:
                    self.error.emit(
                        "Command is still running after {}s with no output — it looks like it's "
                        "blocking in the foreground rather than exiting (raw error: {})."
                        .format(self.timeout, read_err)
                    )
                    return
                try:
                    exit_code = stdout.channel.recv_exit_status()
                except Exception:
                    exit_code = -1
            self.result.emit(out, err, exit_code)
            self.done.emit(out + ("\n[stderr]\n{}".format(err) if err else ""))
        except Exception as e:
            self.error.emit(str(e))

    def _run_raw(self):
        with managed_exec_command(self.ssh, self.cmd) as (_stdin, stdout, stderr):
            stdout.channel.settimeout(self.timeout)
            try:
                out = stdout.read().decode(errors="replace")
                err = stderr.read().decode(errors="replace")
            except Exception as read_err:
                self.error.emit("Command timed out after {}s ({})".format(self.timeout, read_err))
                return
            try:
                exit_code = stdout.channel.recv_exit_status()
            except Exception:
                exit_code = -1
        self.result.emit(out, err, exit_code)
        self.done.emit(out + ("\n[stderr]\n{}".format(err) if err else ""))


class PodExecStreamWorker(QThread):
    """Persistent PTY-backed ``kubectl exec -it`` session with live,
    bidirectional I/O.

    Unlike CommandWorker (which buffers a whole command's output and only
    hands it over when the command exits), this keeps ONE interactive shell
    open inside the pod over an SSH channel that has a pseudo-terminal, and
    emits ``chunk`` the moment bytes arrive — so a long ``wget``/``apt``/
    ``pip`` shows its progress live, exactly like a real terminal.

    ``chunk`` carries the raw terminal stream (escape sequences and all);
    render it with ansi_terminal.AnsiStreamRenderer.
    """

    chunk = pyqtSignal(str)
    exited = pyqtSignal(int)
    error = pyqtSignal(str)

    # Same PATH CommandWorker exports before every remote command. A
    # non-interactive SSH exec doesn't read the login profile, so kubectl
    # living in ~/bin, /usr/local/bin, etc. would otherwise be "not found"
    # even though every other feature in the app finds it. $HOME is
    # expanded by the remote shell, so no separate lookup is needed.
    _PATH_PREFIX = 'export PATH="$HOME:$HOME/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"; '

    def __init__(self, ssh, namespace, pod, container=None, context=None, cols=120, rows=32):
        super().__init__()
        self.ssh = ssh
        self.namespace = namespace
        self.pod = pod
        self.container = container
        self.context = context
        self._cols = max(20, int(cols))
        self._rows = max(5, int(rows))
        self._channel = None
        self._stop = threading.Event()
        self._send_lock = threading.RLock()
        self.finished.connect(self.deleteLater)

    @staticmethod
    def _quote(value):
        return "'" + str(value).replace("'", "'\\''") + "'"

    def _command(self):
        parts = ["exec", "kubectl"]      # `exec`: the shell is replaced, so closing the channel hangs up kubectl itself
        if self.context:
            parts += ["--context", self._quote(self.context)]
        parts += ["exec", "-it", "-n", self._quote(self.namespace)]
        if self.container:
            parts += ["-c", self._quote(self.container)]
        parts += [self._quote(self.pod), "--", "sh"]
        return self._PATH_PREFIX + " ".join(parts)

    def send_input(self, text):
        channel = self._channel
        if channel is None or self._stop.is_set():
            return False
        try:
            data = text.encode("utf-8", errors="replace")
            with self._send_lock:
                channel.sendall(data)
            return True
        except Exception as e:
            self.error.emit(str(e))
            return False

    def interrupt(self):
        return self.send_input("\x03")

    def resize(self, cols, rows):
        """Tell the remote PTY the widget's new size (like resizing a
        terminal window) so progress bars and line wrapping fit."""
        self._cols, self._rows = max(20, int(cols)), max(5, int(rows))
        channel = self._channel
        if channel is not None and not self._stop.is_set():
            try:
                channel.resize_pty(width=self._cols, height=self._rows)
            except Exception:
                pass

    def stop(self):
        self._stop.set()
        channel = self._channel
        if channel is not None:
            try:
                channel.shutdown_write()
            except Exception:
                pass
            try:
                channel.close()
            except Exception:
                pass

    def run(self):
        channel = None
        # A multibyte UTF-8 character (box-drawing progress bars, accents,
        # emoji) can straddle two network reads; decoding each read on its
        # own would turn it into U+FFFD. Incremental decoders carry the
        # partial bytes over to the next read.
        out_dec = codecs.getincrementaldecoder("utf-8")("replace")
        err_dec = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            channel = open_managed_session(self.ssh)
            self._channel = channel
            channel.get_pty(term="xterm-256color", width=self._cols, height=self._rows)
            channel.settimeout(0.2)
            channel.exec_command(self._command())

            while not self._stop.is_set():
                got_data = False
                try:
                    if channel.recv_ready():
                        data = channel.recv(65536)
                        if data:
                            got_data = True
                            text = out_dec.decode(data)
                            if text:
                                self.chunk.emit(text)
                except Exception:
                    if self._stop.is_set():
                        break

                if channel.recv_stderr_ready():
                    try:
                        data = channel.recv_stderr(65536)
                        if data:
                            got_data = True
                            text = err_dec.decode(data)
                            if text:
                                self.chunk.emit(text)
                    except Exception:
                        pass

                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break

                if not got_data:
                    self.msleep(20)

            if not self._stop.is_set():
                try:
                    code = channel.recv_exit_status()
                except Exception:
                    code = -1
                self.exited.emit(code)
        except Exception as e:
            if not self._stop.is_set():
                self.error.emit(str(e))
        finally:
            self._channel = None
            if channel is not None:
                close_managed_session(channel)
