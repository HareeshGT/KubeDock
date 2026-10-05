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

_EXECUTABLE_EXTS = {
    ".py", ".sh", ".bash", ".rb", ".js", ".ts", ".php", ".pl",
    ".lua", ".r", ".go", ".java", ".kt", ".rs", ".c", ".cpp", ".swift",
}
_EDITABLE_KINDS = {"text", "code", "key"}
_MEDIA_KINDS = {"video", "audio"}


class LifecycleMixin:
    def __init__(self):
      super().__init__()
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
      try:
        if self._terminal_popout_win is not None:
          self._terminal_popout_win.close()
        if self._term_ai_worker is not None:
          self._term_ai_worker.quit()
        self.k8s_tab.clear_connection_info() # also stops any active tunnel
        self.dashboard_tab.shutdown() # stop all dashboard timers (focused + background)
        if self._health_worker:
          worker = self._health_worker
          self._health_worker = None
          worker.stop() # don't leave the heartbeat thread running into interpreter teardown
        if self.ssh: close_ssh_connection_pool(self.ssh)
        if self.sftp: self.sftp.close()
        if self.ssh: self.ssh.close()
      except Exception:
        pass
      event.accept()