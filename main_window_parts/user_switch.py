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
from themes import rgba
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

from .dialogs import _UserPickerDialog, UserSwitchDialog


class UserSwitchMixin:
    def _switch_to_user(self, username):
      self.progress.show()
      self.terminal.write_output("Switching context to user: {} …".format(username))
      # 'sudo -n' fails immediately instead of hanging on a password
      # prompt that can never be answered over a non-interactive
      # exec_command channel. The previous version had no check at all —
      # it fired "sudo -u ... echo ~", swallowed any error with
      # '2>/dev/null || echo ""', and unconditionally declared success in
      # _apply_user_switch regardless of whether sudo actually worked.
      # That's exactly the "switch doesn't switch" symptom: the UI says
      # you're now 'adp', but every command underneath silently kept
      # running as the original login user because sudo had quietly
      # failed (no NOPASSWD rule, wrong username, etc).
      check_cmd = "sudo -n -u {} true".format(username)
      worker = CommandWorker(self.ssh, check_cmd)
      worker.done.connect(lambda out: self._on_sudo_check(username, out))
      worker.error.connect(lambda err: self._on_sudo_check(username, err, hard_error=True))
      track_worker(self._workers, worker)
      worker.start()

    def _on_sudo_check(self, username, output, hard_error=False):
      output = (output or "").strip()
      if hard_error or output:
        # 'sudo -n -u <user> true' prints nothing and exits 0 on
        # success. Any output at all here means sudo refused —
        # password required, unknown user, or not permitted by
        # sudoers — so abort instead of pretending it worked.
        self.progress.hide()
        reason = output or "sudo declined the request (no further detail returned)."
        self.terminal.write_output(
          "[sudo mode] Could not switch to '{}':\n{}".format(username, reason)
        )
        self.terminal.show_prompt(self._prompt_str())
        QMessageBox.warning(
          self, "Switch User Failed",
          "Could not switch to user '{}'.\n\n{}\n\n"
          "This usually means sudo requires a password for this action, "
          "or this account isn't permitted (via sudoers) to run commands "
          "as '{}'.".format(username, reason, username)
        )
        return

      # Sudo check passed — now resolve the target's real home directory
      # via getent (authoritative, reads /etc/passwd directly) rather than
      # shell '~' expansion. '~' silently resolves to the *current*
      # user's $HOME if sudo doesn't reset the environment (common when
      # sudoers doesn't have 'always_set_home' enabled), which previously
      # made the terminal show the wrong cwd even when sudo did succeed.
      home_worker = CommandWorker(
        self.ssh,
        "getent passwd {u} 2>/dev/null | cut -d: -f6".format(u=username)
      )
      home_worker.done.connect(lambda out: self._apply_user_switch(username, out.strip()))
      home_worker.error.connect(lambda _: self._apply_user_switch(username, ""))
      track_worker(self._workers, home_worker)
      home_worker.start()

    def _apply_user_switch(self, username, home):
      self.progress.hide()
      if not home or not home.startswith("/"):
        home = "/root" if username == "root" else "/home/{}".format(username)
      self._sudo_user  = username
      self.k8s_tab.set_sudo_user(username)
      self._terminal_cwd = home
      self.sftp.set_sudo_user(username)
      self._update_sudo_badge()
      self.terminal.write_output(
        "[sudo mode] Acting as '{}'.\n"
        "All file operations now run as: sudo -u {}\n"
        "Terminal working directory: {}\n"
        "Type 'exit' or 'su <original_user>' to return to normal.".format(username, username, home)
      )
      self.status.showMessage("sudo → {} • {}".format(username, home))
      self.terminal.show_prompt(self._prompt_str())
      self._nav_to(home)

    def _exit_sudo_mode(self):
      if self.sftp:
        self.sftp.set_sudo_user(None)
      self._sudo_user = None
      self.k8s_tab.set_sudo_user(None)
      with managed_exec_command(self.ssh, "echo $HOME") as (_stdin, _stdout, _stderr):
        self._terminal_cwd = _stdout.read().decode().strip() or None
      self._update_sudo_badge()
      self.terminal.write_output("[sudo mode OFF] Returned to login user.")
      self.terminal.show_prompt(self._prompt_str())
      self.status.showMessage("Returned to login user")
      self._refresh()

    def _update_sudo_badge(self):
      if self._sudo_user:
        self.sudo_badge.setText(" sudo: {} ".format(self._sudo_user))
        self.sudo_badge.setStyleSheet(
          "color: {w}; background: {bg}; "
          "border: 1px solid {bd}; border-radius: 6px; "
          "padding: 1px 6px; font-size: 12px; font-weight: 600;".format(
            w=T['WARNING'], bg=rgba(T['WARNING'], 0.14), bd=rgba(T['WARNING'], 0.35))
        )
        self.sudo_badge.show()
      else:
        self.sudo_badge.hide()
      self._update_switch_user_btn()

    def _update_switch_user_btn(self):
      if self._sudo_user:
        self.switch_user_btn.setText("Exit Sudo ({})".format(self._sudo_user))
        self.switch_user_btn.setToolTip("Return to the original login user")
      else:
        self.switch_user_btn.setText("Switch User")
        self.switch_user_btn.setToolTip("sudo -u <user> — switch context for terminal and file ops")

    def _ctx_menu(self, pos):
      item = self.file_list.itemAt(pos)
      menu = QMenu(self)
      if item:
        meta = item.data(Qt.UserRole)
        if meta["is_dir"]:
          menu.addAction(" Open", lambda: self._nav_to(
            self.current_path.rstrip("/") + "/" + meta["name"]))
        else:
          ext = os.path.splitext(meta["name"])[1].lower()
          kind = meta["kind"]

          if kind in _MEDIA_KINDS:
            menu.addAction("▶ Play", lambda m=meta: self._play_media(m))

          if kind in _EDITABLE_KINDS:
            menu.addAction(" Edit", lambda m=meta: self._edit_file(m))

          if ext in _EXECUTABLE_EXTS or kind == "exec":
            menu.addAction("▶ Run", lambda m=meta: self._exec_file(m))

          menu.addAction(" View", lambda m=meta: self._fetch_preview(
            m["name"], show_dialog=True))
          menu.addAction("⬇ Download", self._download)

        menu.addAction("⌨ Rename", lambda m=meta: self._rename_meta(m))
        menu.addSeparator()
        menu.addAction(" Delete", self._delete)

      menu.addSeparator()
      menu.addAction("⬆ Upload File", self._upload)
      menu.addAction(" New File",  self._new_file)
      menu.addAction(" New Folder", self._new_folder)
      menu.addSeparator()
      menu.addAction("↺ Refresh", self._refresh)
      menu.addSeparator()
      if self._sudo_user:
        menu.addAction(" Exit sudo mode ({})".format(self._sudo_user), self._exit_sudo_mode)
      else:
        menu.addAction(" Switch user (sudo su)…", self._prompt_switch_user)
      menu.exec_(self.file_list.viewport().mapToGlobal(pos))

    def _open_search(self):
      if not self.ssh:
        QMessageBox.information(self, "Search", "Connect to a server first.")
        return
      dlg = SearchDialog(self, self.ssh, start_path=self.current_path)
      dlg.navigate.connect(self._nav_to)
      dlg.exec_()

    def _prompt_switch_user(self):
      if not self.ssh:
        QMessageBox.information(
          self,
          "Switch User",
          "Connect to a server first."
        )
        return

      self.progress.show()

      # Get all accounts from the remote machine.
      worker = CommandWorker(
        self.ssh,
        "getent passwd 2>/dev/null"
      )

      worker.done.connect(self._on_switch_users_loaded)
      worker.error.connect(self._on_switch_users_error)

      track_worker(self._workers, worker)
      worker.start()

    def _on_switch_users_loaded(self, output):
      self.progress.hide()

      users = []

      for line in (output or "").splitlines():
        parts = line.split(":", 6)

        if not parts:
          continue

        username = parts[0].strip()

        if not username:
          continue

        # Ignore malformed entries.
        if len(parts) < 7:
          continue

        users.append(username)

      # Remove duplicates while preserving order.
      users = list(dict.fromkeys(users))

      if not users:
        QMessageBox.information(
          self,
          "Switch User",
          "No users were found on the remote server."
        )
        return

      # Determine the currently logged-in / active user.
      current_user = self._sudo_user

      if not current_user:
        try:
          current_user = self.username
        except AttributeError:
          current_user = None

      dlg = UserSwitchDialog(
        self,
        users,
        current_user=current_user
      )

      if dlg.exec_() == QDialog.Accepted:
        username = dlg.selected_user()

        if username:
          self._switch_to_user(username)

    def _on_switch_users_error(self, error):
      self.progress.hide()

      QMessageBox.warning(
        self,
        "Switch User",
        "Could not retrieve the users from the server.\n\n"
        + str(error)
      )

    def _on_users_listed(self, output):
      self.progress.hide()
      self.status.clearMessage()
      users = []
      for line in (output or "").splitlines():
        name, sep, uid = line.strip().partition(":")
        if name and sep:
          users.append((name, uid))
      if not users:
        self._prompt_switch_user_manual()
        return
      dlg = _UserPickerDialog(self, users, current=self._sudo_user)
      if dlg.exec_() == QDialog.Accepted and dlg.selected_user:
        self._switch_to_user(dlg.selected_user)

    def _prompt_switch_user_manual(self):
      self.progress.hide()
      self.status.showMessage("Could not list system users — enter manually")
      username, ok = QInputDialog.getText(self, "Switch User", "Username:")
      if ok and username.strip():
        self._switch_to_user(username.strip())

    def _on_switch_user_btn(self):
      # Same toggle behavior as the context-menu entries: prompt for a
      # username when not currently sudo'd, exit sudo mode when we are.
      if self._sudo_user:
        self._exit_sudo_mode()
      else:
        self._prompt_switch_user()

