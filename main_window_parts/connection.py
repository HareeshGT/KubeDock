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
    CommandWorker, ConnectWorker, FTPConnectionWorker, EFSConnectionWorker, ConnectionHealthWorker,
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


class ConnectionMixin:
    def _connect(self):
      dlg = ConnectDialog(self)
      if dlg.exec_() != QDialog.Accepted:
        return

      efs_values = dlg.efs_values()
      if efs_values:
        self._connect_efs(efs_values)
        return

      protocol, host, port, user, pem, password, alias = dlg.values()
      if not host or not user:
        QMessageBox.warning(self, "Missing info", "Host and username are required.")
        return

      effective_pem = pem if not password else ""
      self._pending_conn = dict(protocol=protocol, host=host, port=port, user=user,
                   pem=effective_pem, password=password, alias=alias)

      self.progress.show()
      self.status.showMessage("Connecting to {}…".format(host))
      self._connecting_dlg = ConnectingDialog(self, host)
      self._connecting_dlg.show()

      if protocol == "ssh":
        self._connect_worker = ConnectWorker(host, port, user, pem, password)
        self._connect_worker.connected.connect(self._on_connect_success)
      else:
        self._connect_worker = FTPConnectionWorker(
          host, port, user, password, tls=(protocol == "ftps"), passive=True
        )
        self._connect_worker.connected.connect(self._on_ftp_connect_success)
      self._connect_worker.error.connect(self._on_connect_error)
      self._connect_worker.start()

    def _connect_efs(self, values):
      region = values.get("region")
      profile = values.get("profile")
      filesystem_id = values.get("filesystem_id")
      if not region or not filesystem_id:
        QMessageBox.warning(self, "AWS EFS", "Region and filesystem are required.")
        return

      self._pending_conn = dict(
        protocol="efs",
        region=region,
        profile=profile,
        filesystem_id=filesystem_id,
      )
      self.progress.show()
      self.status.showMessage("Mounting EFS {}…".format(filesystem_id))
      self._connecting_dlg = ConnectingDialog(self, "AWS EFS {}".format(filesystem_id))
      self._connecting_dlg.show()

      self._connect_worker = EFSConnectionWorker(region, profile, filesystem_id)
      self._connect_worker.connected.connect(self._on_efs_connect_success)
      self._connect_worker.error.connect(self._on_connect_error)
      self._connect_worker.start()

    def _on_efs_connect_success(self, manager, mount_path, filesystem_id):
      if self.ssh is not None or self.sftp is not None:
        self._disconnect(reason="Switching to AWS EFS")

      from local_fs import LocalFS
      self._efs_manager = manager
      self._efs_mount_path = mount_path
      self.ssh = None
      self.sftp = LocalFS(mount_path)
      self._conn_protocol = "efs"
      self.host_label = "EFS {}".format(filesystem_id)
      self._conn_host = None
      self._conn_port = 0
      self._conn_user = None
      self._conn_pem = None
      self._conn_password = None
      self._sudo_user = None
      self._set_connected(True)
      self.k8s_tab.set_ssh(None)
      self.k8s_tab.clear_connection_info()
      self.dashboard_tab.set_ssh(None)
      self.sidebar.populate_remote(self.sftp)
      self._nav_to(mount_path)
      self._terminal_cwd = mount_path
      self.terminal.clear()
      self.terminal.write_output("Connected to AWS EFS {}.".format(filesystem_id))
      self.terminal.show_prompt("(EFS) $ ")
      self.status.showMessage("EFS {} mounted at {}".format(filesystem_id, mount_path))
      self._finish_connect_ui()

    def _on_ftp_connect_success(self, fs, home):
      info = self._pending_conn
      host, port, user = info["host"], info["port"], info["user"]
      protocol = info["protocol"]
      self.ssh = None
      self.sftp = fs
      self._conn_protocol = protocol
      self.host_label = info["alias"] if info["alias"] else "{}@{}".format(user, host)
      self._conn_host, self._conn_port = host, port
      self._conn_user, self._conn_pem = user, ""
      self._conn_password = info.get("password")
      self._sudo_user = None
      self._set_connected(True)
      self.k8s_tab.set_ssh(None)
      self.dashboard_tab.set_connection(protocol, fs=fs, host=host, port=port, user=user)
      self.k8s_tab.clear_connection_info()
      self.sidebar.populate_remote(self.sftp)
      add_recent_instance(host, port, user, "", info["alias"], protocol)
      self._nav_to(home or "/")
      self._terminal_cwd = None
      self.terminal.clear()
      self.terminal.write_output("Connected to {} via {}.".format(self.host_label, protocol.upper()))
      self.terminal.show_prompt("(FTP) $ ")
      self.status.showMessage("Connected successfully")
      self._finish_connect_ui()

    def _on_connect_success(self, ssh, sftp, home):
      # Defensive: if a session is somehow still live (e.g. a connect that
      # completed after the user reconnected), tear it down fully first so
      # its pool connections and heartbeat thread aren't orphaned.
      if self.ssh is not None and self.ssh is not ssh:
        self._disconnect(reason="Reconnecting")
      info = self._pending_conn
      host, port, user, pem, alias = info["host"], info["port"], info["user"], info["pem"], info["alias"]
      password = info.get("password")

      self.ssh = ssh
      self.sftp = SudoFS(sftp, ssh)
      self._conn_protocol = "ssh"
      # Use alias as the display label when set, else user@host
      self.host_label = alias if alias else "{}@{}".format(user, host)
      self._conn_host, self._conn_port = host, port
      self._conn_user, self._conn_pem = user, pem
      # Kept in memory for the rest of the session so uploads/downloads
      # can use the fast scp path even on a password-authenticated
      # connection — same trust boundary as the ConnectDialog holding it
      # in a QLineEdit for the seconds it takes to connect, just longer.
      self._conn_password = password
      self._set_connected(True)
      self.k8s_tab.set_ssh(self.ssh)
      self.k8s_tab.set_sudo_user(None)
      self.dashboard_tab.set_connection("ssh", ssh=self.ssh, host=host, port=port, user=user)
      # Local (client-side) connection details for the port-tunnel
      # feature, which runs `ssh` on this machine rather than over
      # the remote self.ssh session.
      self.k8s_tab.set_connection_info(host, port, user, pem)
      self.sidebar.populate_remote(self.sftp)
      # Pass alias to persist it in the CSV
      add_recent_instance(host, port, user, pem, alias, "ssh")
      self._nav_to("")
      self._terminal_cwd = home or None
      self.terminal.clear()
      self.terminal.write_output("Connected to {}.".format(self.host_label))
      self.terminal.show_prompt(self._prompt_str())
      self.status.showMessage("Connected successfully")
      self._finish_connect_ui()

      # Start the background heartbeat so the app can warn about — and
      # ideally recover the UI cleanly from — a dying connection instead
      # of only discovering it when some unrelated action fails midway.
      self._health_worker = ConnectionHealthWorker(self.ssh)
      self._health_worker.at_risk.connect(self._on_connection_at_risk)
      self._health_worker.recovered.connect(self._on_connection_recovered)
      self._health_worker.lost.connect(self._on_connection_lost)
      self._health_worker.start()

    def _on_connection_at_risk(self, detail):
      # A single missed heartbeat — could be a transient network blip, so
      # this is deliberately a non-blocking heads-up (status bar + the
      # connection indicator turning amber), not a modal dialog. Give the
      # user a chance to finish and save whatever they're doing.
      if not self.ssh:
        return
      self.conn_lbl.setText("Unstable — {} ".format(self.host_label))
      self.conn_lbl.setStyleSheet("color: {};".format(T['WARNING']))
      self.status.showMessage(
        "Connection to {} looks unstable — it may drop soon.".format(self.host_label),
        8000,
      )

    def _on_connection_recovered(self):
      if not self.ssh:
        return
      self.conn_lbl.setText("Connected — {}".format(self.host_label))
      self.conn_lbl.setStyleSheet("color: {};".format(T['SUCCESS']))
      self.status.showMessage("Connection to {} recovered.".format(self.host_label), 5000)

    def _on_connection_lost(self, detail):
      # The heartbeat confirmed the transport is actually dead. This can
      # still fire once, harmlessly, right after a deliberate manual
      # disconnect (a check already in flight when the socket closes) —
      # self.ssh is cleared first in _disconnect(), so that race is a
      # no-op here.
      if not self.ssh:
        return
      host_label = self.host_label
      self._disconnect(reason="Connection lost")
      QMessageBox.warning(
        self, "Connection Lost",
        "The SSH connection to {} was lost:\n\n{}".format(host_label, detail)
      )

    def _on_connect_error(self, message):
      QMessageBox.critical(self, "Connection Failed", message)
      self.status.showMessage("Connection failed")
      self._finish_connect_ui()

    def _finish_connect_ui(self):
      if getattr(self, "_connecting_dlg", None):
        self._connecting_dlg.hide()
        self._connecting_dlg.deleteLater()
        self._connecting_dlg = None
      self.progress.hide()

    def _disconnect(self, reason="Disconnected"):
      # Stop the heartbeat first — and clear self.ssh right after closing
      # — so a health check already in flight can't deliver a stray
      # "lost" signal for a connection we're closing on purpose (see the
      # guard in _on_connection_lost).
      if self._health_worker:
        worker = self._health_worker
        self._health_worker = None
        worker.stop()
        # Stop the heartbeat asynchronously; never block the UI during disconnect.
      # Close the secondary-connection pool BEFORE the primary client. The
      # pool's connections are separate TCP sessions with their own
      # keepalive threads; closing only self.ssh used to leave every one of
      # them open on the server until the process exited.
      try:
        if self.ssh: close_ssh_connection_pool(self.ssh)
      except Exception:
        pass
      try:
        if self.sftp: self.sftp.close()
        if self.ssh: self.ssh.close()
      except Exception:
        pass
      try:
        if self._efs_manager:
          self._efs_manager.close()
      except Exception:
        pass
      self._efs_manager = None
      self._efs_mount_path = None
      self.ssh = self.sftp = None
      self._sudo_user = None
      self._conn_host = self._conn_user = self._conn_pem = None
      self._conn_password = None
      self._conn_protocol = "ssh"
      self._conn_port = 22
      self.k8s_tab.set_ssh(None)
      self.k8s_tab.clear_connection_info()
      self.dashboard_tab.set_ssh(None)
      self.sidebar.clear_remote()
      self.file_list.clear()
      self._items = []
      self.preview.clear()
      self._set_connected(False)
      self.sudo_badge.hide()
      self.addr_bar.clear()
      self.terminal.write_output("[disconnected]")
      self.terminal.show_prompt("(not connected)$ ")
      self.status.showMessage(reason)