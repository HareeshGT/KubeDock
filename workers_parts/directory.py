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

class DirectoryListWorker(QThread):
    """Read remote directory metadata outside the Qt UI thread."""

    result = pyqtSignal(object, str)
    error = pyqtSignal(str)

    def __init__(self, sftp, path):
        super().__init__()
        self.sftp = sftp
        self.path = path
        self._stop = threading.Event()
        self.finished.connect(self.deleteLater)

    def stop(self):
        self._stop.set()

    def _session_is_current(self):
        guard = getattr(self, "_session_guard", None)
        if guard is None:
            return True
        try:
            return bool(guard())
        except Exception:
            return False

    def run(self):
        try:
            if not self._session_is_current():
                return
            entries = self.sftp.listdir_attr(self.path)
            result = []
            for entry in entries:
                if self._stop.is_set() or not self._session_is_current():
                    return
                mode = entry.st_mode or 0
                is_dir = stat.S_ISDIR(mode)
                if stat.S_ISLNK(mode):
                    try:
                        target = self.path.rstrip("/") + "/" + entry.filename
                        is_dir = stat.S_ISDIR(self.sftp.stat(target).st_mode or 0)
                    except Exception:
                        is_dir = False
                result.append({
                    "name": entry.filename,
                    "size": entry.st_size or 0,
                    "mode": mode,
                    "is_dir": is_dir,
                })
            if not self._stop.is_set() and self._session_is_current():
                self.result.emit(result, self.path)
        except Exception as exc:
            if not self._stop.is_set():
                self.error.emit(str(exc))
