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

from .dialogs import _TerminalPopoutWindow


class TerminalMixin:
    def _detect_user_switch(self, cmd):
      c = cmd.strip()
      m = re.match(r"^(?:sudo\s+)?su(?:\s+(?:-\s*|--login\s*))?(?:\s+(\w+))?$", c)
      if m:
        return m.group(1) or "root"
      m = re.match(r"^sudo\s+(?:-\w+\s+)*-u\s+(\w+)", c)
      if m:
        return m.group(1)
      if re.match(r"^sudo\s+(?:-\w+\s+)*-i\b", c):
        return "root"
      return None

    def _is_exit_cmd(self, cmd):
      return cmd.strip().lower() in ("exit", "logout", "quit")

    def _prompt_str(self) -> str:
      who = self._sudo_user or "me"
      host = self.host_label or "remote"
      cwd = self._terminal_cwd or "~"
      return "{}@{}:{}$ ".format(who, host, cwd)

    def _on_terminal_command(self, cmd):
      """Called when the user presses Enter at the live prompt in the
      integrated terminal widget (terminal_widget.py)."""
      cmd = cmd.strip()
      if not self.ssh:
        self.terminal.write_output("Not connected.")
        self.terminal.show_prompt(self._prompt_str())
        return
      if not cmd:
        self.terminal.show_prompt(self._prompt_str())
        return

      if self._is_exit_cmd(cmd) and self._sudo_user:
        self._exit_sudo_mode()
        return

      if cmd.lower() in ("clear", "cls"):
        # A real screen-clear is a terminal-emulator action, not remote
        # command output — running it over a non-PTY SSH exec_command
        # either fails ("TERM environment variable not set") or, even
        # if it succeeded, would just print raw ANSI codes as text
        # since this isn't a full VT100 emulator. Handle it locally.
        self.terminal.clear()
        self.terminal.show_prompt(self._prompt_str())
        return

      target_user = self._detect_user_switch(cmd)
      if target_user:
        self._switch_to_user(target_user)
        return

      self.progress.show()

      worker = (
        self.services.terminal.create_command_worker(
          None, cmd, cwd=self._terminal_cwd, sudo_user=self._sudo_user
        )
        if getattr(self, "services", None) is not None
        else CommandWorker(self.ssh, cmd, cwd=self._terminal_cwd, sudo_user=self._sudo_user)
      )
      worker.result.connect(lambda out, err, code, cmd=cmd: self._on_terminal_result(cmd, out, err, code))
      worker.done.connect(lambda out: self._cmd_done(out, cmd))
      worker.error.connect(lambda e: self._cmd_done("[error] {}".format(e), cmd))
      track_worker(self._workers, worker)
      worker.start()

    def _on_terminal_result(self, cmd, out, err, exit_code):
      """Remembers the last terminal command's real exit code/stdout/
      stderr for the Explain button — see ExecDialog._on_cmd_result
      in dialogs.py for the equivalent in the kubectl-exec terminal."""
      self._last_term_cmd    = cmd
      self._last_term_stdout  = out
      self._last_term_stderr  = err
      self._last_term_exit_code = exit_code
      failed = exit_code != 0 or bool((err or "").strip())
      self.terminal_explain_btn.setEnabled(failed)

    def _on_terminal_explain(self):
      if self._term_ai_worker is not None or self._last_term_cmd is None:
        return # already running, or nothing to explain yet

      provider = ai_assist.get_provider()
      api_key = ai_assist.get_api_key(provider)
      if not api_key:
        label = ai_assist.PROVIDERS.get(provider, {}).get("label", provider)
        QMessageBox.information(
          self, "No API key set",
          f"Add a {label} API key in Settings → AI to use this feature."
        )
        return

      self._term_ai_dialog = AIExplainDialog(self, f"AI diagnosis — {self._last_term_cmd}")
      self._term_ai_dialog.set_loading()
      self._term_ai_dialog.show()

      self.terminal_explain_btn.setEnabled(False)

      worker = (
        self.services.ai.create_command_explain_worker(
          self._last_term_cmd, self._last_term_exit_code,
          self._last_term_stderr, self._last_term_stdout,
        )
        if getattr(self, "services", None) is not None
        else ai_assist.AICommandExplainWorker(
          provider, api_key, ai_assist.get_model(provider),
          self._last_term_cmd, self._last_term_exit_code,
          self._last_term_stderr, self._last_term_stdout,
        )
      )
      worker.done.connect(self._on_terminal_explain_done)
      worker.error.connect(self._on_terminal_explain_error)
      worker.finished.connect(self._on_terminal_explain_finished)
      self._term_ai_worker = worker
      worker.start()

    def _on_terminal_explain_done(self, text):
      if self._term_ai_dialog is not None:
        self._term_ai_dialog.set_markdown(text)

    def _on_terminal_explain_error(self, message):
      if self._term_ai_dialog is not None:
        self._term_ai_dialog.set_error(message)

    def _on_terminal_explain_finished(self):
      self._term_ai_worker = None
      failed = (self._last_term_exit_code not in (0, None)) or bool((self._last_term_stderr or "").strip())
      self.terminal_explain_btn.setEnabled(failed)

    def _on_terminal_interrupt(self):
      # Commands here run one-shot over SSH exec_command (not a live PTY
      # channel), so there's no live process to actually signal — just
      # let the user know rather than pretending to interrupt anything.
      self.terminal.write_output("^C (nothing to interrupt — commands run to completion over SSH)")

    def _toggle_terminal_popout(self):
      if self._terminal_popout_win is not None:
        self._terminal_popout_win.close()  # triggers _redock_terminal via closeEvent
      else:
        self._popout_terminal()

    def _popout_terminal(self):
      win = _TerminalPopoutWindow(self, on_close=self._redock_terminal)
      layout = QVBoxLayout(win)
      layout.setContentsMargins(0, 0, 0, 0)
      layout.setSpacing(0)

      self._terminal_home_layout.removeWidget(self.terminal)
      layout.addWidget(self.terminal)
      self.terminal.show()

      self.terminal_dock_placeholder.show()
      self.terminal_popout_btn.setText("⤡")
      self.terminal_popout_btn.setToolTip("Bring terminal back into the main window")

      self._terminal_popout_win = win
      win.show()
      win.raise_()
      self.terminal.setFocus()

    def _redock_terminal(self):
      if self._terminal_popout_win is None:
        return
      win = self._terminal_popout_win
      self._terminal_popout_win = None

      self.terminal_dock_placeholder.hide()
      # Insert the terminal back above the placeholder, in its original spot.
      idx = self._terminal_home_layout.indexOf(self.terminal_dock_placeholder)
      self._terminal_home_layout.insertWidget(max(idx, 0), self.terminal)
      self.terminal.show()

      self.terminal_popout_btn.setText("⤢")
      self.terminal_popout_btn.setToolTip("Open terminal in its own window")

      win.deleteLater()

    def _update_cwd_after_cmd(self, cmd):
      if not re.search(r'(?:^|[;&|])\s*cd\b', cmd):
        return False
      cwd_prefix = "cd {} 2>/dev/null; ".format(self._terminal_cwd) if self._terminal_cwd else ""
      full    = "{}{}> /dev/null 2>&1; pwd".format(cwd_prefix, cmd + " ")
      worker   = (
        self.services.terminal.create_command_worker(
          None, full, sudo_user=self._sudo_user
        )
        if getattr(self, "services", None) is not None
        else CommandWorker(self.ssh, full, sudo_user=self._sudo_user)
      )

      def on_done(out):
        self._set_terminal_cwd(out.strip().splitlines()[-1] if out.strip() else "")
        self.terminal.show_prompt(self._prompt_str())

      def on_error(_e):
        # Couldn't confirm the new directory — show a prompt anyway
        # (with whatever cwd we last knew) rather than leaving the
        # terminal stuck waiting forever.
        self.terminal.show_prompt(self._prompt_str())

      worker.done.connect(on_done)
      worker.error.connect(on_error)
      track_worker(self._cwd_workers, worker)
      worker.start()
      return True

    def _set_terminal_cwd(self, path):
      if path and path.startswith("/"):
        self._terminal_cwd = path

    def _cmd_done(self, result, cmd=""):
      self.progress.hide()
      self.terminal.write_output(result)
      self.status.showMessage("Command finished")
      # If this was a 'cd', wait for the async pwd check to resolve before
      # showing the next prompt — otherwise it prints with the stale cwd.
      if cmd and self._update_cwd_after_cmd(cmd):
        return
      self.terminal.show_prompt(self._prompt_str())

