"""Generation-aware SSH command stream worker shared by file and feature services."""

from PyQt5.QtCore import QThread, pyqtSignal

from .ssh import open_managed_session, close_managed_session


class ExecStreamWorker(QThread):
    """Stream PTY-backed SSH command output without depending on a dialog module."""

    line = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(int)

    def __init__(self, ssh, cmd: str):
        super().__init__()
        self._ssh = ssh
        self._cmd = cmd
        self._stop = False
        self._channel = None
        self._session_guard = None
        self.finished.connect(self.deleteLater)

    def _session_is_current(self):
        guard = self._session_guard
        if guard is None:
            return True
        try:
            return bool(guard())
        except Exception:
            return False

    def request_stop(self):
        self._stop = True
        channel = self._channel
        if channel is not None:
            try:
                channel.close()
            except Exception:
                pass

    cancel = request_stop

    def send_input(self, text: str):
        """Write input to the active PTY; safe to call from the UI thread."""
        channel = self._channel
        if channel is None:
            return
        try:
            channel.send(text)
        except Exception:
            pass

    def run(self):
        channel = None
        exit_code = -1
        try:
            if self._stop:
                return
            if not self._session_is_current():
                self.error.emit("The active connection changed before the stream could start.")
                return
            channel = open_managed_session(self._ssh)
            channel.get_pty()
            channel.settimeout(0.5)
            channel.exec_command(self._cmd)
            self._channel = channel

            while True:
                if self._stop:
                    return
                if not self._session_is_current():
                    self.error.emit("The active connection changed while the stream was running.")
                    return
                try:
                    if channel.recv_ready():
                        chunk = channel.recv(4096)
                        if chunk:
                            self.line.emit(chunk.decode("utf-8", errors="replace"))
                        continue
                except Exception:
                    if self._stop:
                        return
                if channel.exit_status_ready() and not channel.recv_ready():
                    break
                self.msleep(50)

            exit_code = channel.recv_exit_status() if not self._stop else -1
        except Exception as exc:
            if not self._stop:
                self.error.emit(str(exc))
            exit_code = -1
        finally:
            self._channel = None
            close_managed_session(channel)
            self.finished.emit(exit_code)
