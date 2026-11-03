from __future__ import annotations

from typing import Optional
import os
import re
import stat
import codecs

import paramiko
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QLineEdit, QLabel,
    QMessageBox, QTextEdit, QSplitter, QStatusBar, QFrame,
    QSizePolicy, QDialog, QDialogButtonBox, QInputDialog, QMenu,
    QAbstractItemView, QProgressBar, QTabWidget, QComboBox,
    QApplication, QShortcut, QGraphicsOpacityEffect, QGraphicsDropShadowEffect,
)
from PyQt5.QtCore import (
    Qt, QSize, QPropertyAnimation, QEasingCurve, QRect, QPoint, QTimer,
    QParallelAnimationGroup, QEvent,
)
from PyQt5.QtGui import QFont, QColor, QPalette, QKeySequence

import themes as _themes
from ui_icons import set_icon, apply_text_icon, add_icon_tab, icon_button, icon_pixmap, split_icon_text
from themes import T, THEMES, apply_theme_vars, build_qss, apply_qss_to, save_settings
from utils import classify, icon_for, size_fmt, add_recent_instance, monospace_font
from sudo_fs import SudoFS
from ftp_fs import FTPFS
from workers import (
    CommandWorker, ConnectWorker, FTPConnectionWorker, ConnectionHealthWorker,
    FileStreamReadWorker, DirectoryListWorker, track_worker, managed_exec_command,
    close_ssh_connection_pool,
)
from dialogs import (
    ConnectDialog, FileTransferDialog, FileEditorDialog, FileExecDialog,
    SearchDialog, ConnectingDialog, MediaPlayerDialog, AIExplainDialog,
)
from large_file_viewer import LargeFileViewerDialog
import ai_assist
from sidebar import Sidebar
from preview import PreviewPane
from file_widgets import FileRowWidget, FileGridWidget
from terminal_widget import TerminalWidget
from kubernetes_tab import KubernetesTab
from dashboard_tab import DashboardTab
from theme_picker import ThemePicker
from settings_dialog import SettingsDialog
from lock_screen import AppLockDialog
import security

from services import AppServices

_EXECUTABLE_EXTS = {
    ".py", ".sh", ".bash", ".rb", ".js", ".ts", ".php", ".pl",
    ".lua", ".r", ".go", ".java", ".kt", ".rs", ".c", ".cpp", ".swift",
}
_EDITABLE_KINDS = {"text", "code", "key"}
_MEDIA_KINDS = {"video", "audio"}


class LifecycleMixin:
    @property
    def ssh(self):
      services = getattr(self, "services", None)
      if services is not None:
        return services.connection.get_ssh_client()
      return getattr(self, "_ssh_compat", None)

    @ssh.setter
    def ssh(self, value):
      # Compatibility storage for partially initialized windows. Once the
      # service container exists, the ConnectionService is authoritative.
      self._ssh_compat = value

    @property
    def sftp(self):
      services = getattr(self, "services", None)
      if services is not None:
        return services.connection.get_filesystem()
      return getattr(self, "_sftp_compat", None)

    @sftp.setter
    def sftp(self, value):
      self._sftp_compat = value

    def __init__(self):
      super().__init__()
      # All feature modules share this process-local service composition root.
      self.services = AppServices()
      self.connection_service = self.services.connection
      self.ssh      = None
      self.sftp     = None
      self._health_worker = None
      self._preview_worker = None # FileStreamReadWorker backing _fetch_preview
      self._directory_worker = None
      self._directory_refresh_pending = None
      self._directory_generation = 0
      self._row_population_generation = 0
      self.current_path = "/"
      self.history    = []
      self.future    = []
      self._items    = []
      self.host_label  = ""
      self._sudo_user  = None # type: Optional[str]
      # Local (client-side) connection details, kept around so file
      # transfers can shell out to the system `scp` binary directly
      # instead of going through paramiko's SFTP implementation.
      self._conn_host  = None # type: Optional[str]
      self._conn_port  = 22
      self._conn_user  = None # type: Optional[str]
      self._conn_pem   = None # type: Optional[str]
      self._conn_password = None # type: Optional[str]
      self._conn_protocol = "ssh"
      self._terminal_cwd = None # type: Optional[str]
      # "Analyze with AI" AI feature state for the plain SSH terminal —
      # mirrors ExecDialog's equivalent state (see dialogs.py).
      self._last_term_cmd    = None
      self._last_term_stdout  = ""
      self._last_term_stderr  = ""
      self._last_term_exit_code = None
      self._term_ai_worker   = None
      self._term_ai_dialog   = None
      self._theme_fade_anim = None # keeps the QPropertyAnimation alive while running
      self._pending_conn = {}
      self._connecting_dlg = None
      self._connect_worker = None
      self.view_mode   = "list"
      self.sort_key   = "Name"
      self.sort_reverse = False
      self._workers   = []
      self._cwd_workers = []

      # Tab-switch slide animation state — see _animate_tab_slide().
      self._tab_anim_group  = None
      self._tab_anim_overlays = []
      self._current_tab_widget = None
      self._current_tab_idx  = -1
      self._tab_slide_ready  = False

      self.setWindowTitle("KubeDock")
      self.resize(1260, 740)
      apply_qss_to(self)
      self._build_ui()
      # _build_ui() adds tabs one at a time, which fires currentChanged
      # (main_tabs) synthetically several times (-1→0→…) before there's
      # anything meaningful to slide between. Only start animating after
      # the tab set has settled, and seed the "current" tracking from
      # wherever _build_ui() landed (the Dashboard tab).
      self._current_tab_widget = self.main_tabs.currentWidget()
      self._current_tab_idx  = self.main_tabs.currentIndex()
      self._tab_slide_ready  = True
      self._set_connected(False)
      self.terminal.show_prompt("(not connected)$ ")

      self._inactivity_watcher = None
      self._lock_dlg_open   = False
      self._lock_pending    = False
      self._setup_app_lock()

    def closeEvent(self, event):
      # Keep teardown best-effort, but isolate each step. A failure while
      # closing a popout or stopping a dashboard timer must never skip the
      # shared connection service shutdown and leak the active SSH/FTP session.
      def _cleanup(action):
        try:
          action()
        except Exception:
          pass

      popout = getattr(self, "_terminal_popout_win", None)
      if popout is not None:
        _cleanup(popout.close)

      term_ai_worker = getattr(self, "_term_ai_worker", None)
      if term_ai_worker is not None:
        _cleanup(term_ai_worker.quit)

      k8s_tab = getattr(self, "k8s_tab", None)
      if k8s_tab is not None:
        # Also stops any active tunnel before the shared session is closed.
        _cleanup(k8s_tab.clear_connection_info)

      dashboard_tab = getattr(self, "dashboard_tab", None)
      if dashboard_tab is not None:
        # Stop focused and background dashboard timers.
        _cleanup(dashboard_tab.shutdown)

      health_worker = getattr(self, "_health_worker", None)
      self._health_worker = None
      if health_worker is not None:
        # Don't leave the heartbeat thread running into interpreter teardown.
        _cleanup(health_worker.stop)

      # ConnectionService owns the primary session and managed secondary SSH
      # pool. Always attempt its idempotent teardown, regardless of failures
      # in UI cleanup above.
      services = getattr(self, "services", None)
      if services is not None:
        _cleanup(services.shutdown)

      _cleanup(lambda: setattr(self, "ssh", None))
      _cleanup(lambda: setattr(self, "sftp", None))
      event.accept()