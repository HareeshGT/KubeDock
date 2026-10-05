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


class ThemeSecurityMixin:
    def _change_theme(self, theme_name):
      """Cross-fade into the new theme instead of slamming every colour
      in on a single frame — restyling the whole window (QSS, palette,
      every tab) happens instantly either way, so the fade is what
      makes the switch itself read as smooth rather than a jump-cut."""
      central = self.centralWidget()

      # A second theme pick while one is still fading: finish the style
      # swap for the theme already in flight, then start this one clean
      # rather than layering animations on top of each other.
      if self._theme_fade_anim is not None:
        self._theme_fade_anim.stop()
        self._theme_fade_anim = None

      effect = QGraphicsOpacityEffect(central)
      central.setGraphicsEffect(effect)

      fade_out = QPropertyAnimation(effect, b"opacity", central)
      fade_out.setDuration(90)
      fade_out.setStartValue(1.0)
      fade_out.setEndValue(0.12)
      fade_out.setEasingCurve(QEasingCurve.OutCubic)

      def _swap_and_fade_in():
        self._apply_theme_change(theme_name)

        fade_in = QPropertyAnimation(effect, b"opacity", central)
        fade_in.setDuration(180)
        fade_in.setStartValue(0.12)
        fade_in.setEndValue(1.0)
        fade_in.setEasingCurve(QEasingCurve.InCubic)

        def _teardown():
          # Drop the effect once it's done its job — leaving a
          # QGraphicsOpacityEffect attached permanently forces every
          # repaint of the whole window through an extra compositing
          # pass for no benefit once opacity is back to 1.0.
          central.setGraphicsEffect(None)
          self._theme_fade_anim = None

        fade_in.finished.connect(_teardown)
        self._theme_fade_anim = fade_in
        fade_in.start()

      fade_out.finished.connect(_swap_and_fade_in)
      self._theme_fade_anim = fade_out
      fade_out.start()

    def _apply_theme_change(self, theme_name):
      """The actual restyle: swap the palette dict, rebuild QSS, and push
      refreshed styles into every tab. Runs at the faded-out midpoint of
      _change_theme's cross-fade so the color jump itself is hidden."""
      apply_theme_vars(theme_name)
      save_settings()
      qss = build_qss()
      self.setStyleSheet(qss)

      app = QApplication.instance()
      pal = QPalette()
      pal.setColor(QPalette.Window,     QColor(T['BG_DARK']))
      pal.setColor(QPalette.WindowText,   QColor(T['TEXT_PRIMARY']))
      pal.setColor(QPalette.Base,      QColor(T['BG_PANEL']))
      pal.setColor(QPalette.AlternateBase,  QColor(T['BG_ITEM']))
      pal.setColor(QPalette.Text,      QColor(T['TEXT_PRIMARY']))
      pal.setColor(QPalette.Button,     QColor(T['BG_ITEM']))
      pal.setColor(QPalette.ButtonText,   QColor(T['TEXT_PRIMARY']))
      pal.setColor(QPalette.Highlight,    QColor(T['ACCENT']))
      pal.setColor(QPalette.HighlightedText, QColor("#ffffff"))
      app.setPalette(pal)

      self._apply_inline_styles()
      self.sidebar.refresh_theme()
      self.nav_rail.refresh_theme()
      self.preview.refresh_theme()
      self.k8s_tab.apply_theme()
      self.dashboard_tab.apply_theme()
      self._rebuild_list_header()
      self.theme_picker.refresh_theme()

      if self.sftp:
        self._refresh(push_history=True)

      self.status.showMessage("Theme changed to {}".format(theme_name))

    def _open_settings(self):
      dlg = SettingsDialog(self, k8s_tab_titles=self.k8s_tab.visible_tab_titles())
      if dlg.exec_() != QDialog.Accepted:
        return

      self.k8s_tab.apply_hidden_tabs(dlg.hidden_k8s_tabs())

      lock_settings = security.get_lock_settings()
      if self._inactivity_watcher:
        self._inactivity_watcher.set_minutes(lock_settings["autolock_minutes"])
        if lock_settings["enabled"]:
          self._inactivity_watcher.resume()
        else:
          self._inactivity_watcher.suspend()

      self.status.showMessage("Settings saved")

    def _setup_app_lock(self):
      """Installs the app-wide inactivity watcher. Does NOT show the
      lock screen at launch — see check_initial_lock() for why that's
      kept separate."""
      settings = security.get_lock_settings()
      self._inactivity_watcher = security.InactivityWatcher(settings["autolock_minutes"])
      self._inactivity_watcher.idle_timeout.connect(self._show_lock_screen)
      QApplication.instance().installEventFilter(self._inactivity_watcher)
      # The watcher's eventFilter only ever sees events aimed at this
      # app's own widgets — if the user switches to another app, we
      # receive nothing at all, so the idle clock silently keeps
      # counting from the moment they left. When it then fires,
      # _show_lock_screen() used to unconditionally build an
      # always-on-top AppLockDialog and exec() it, which popped up and
      # stole focus over whatever app the user was actually using.
      # Tracking real focus via applicationStateChanged lets us defer
      # that popup until the user is actually back in this app.
      QApplication.instance().applicationStateChanged.connect(self._on_app_state_changed)

      if not (settings["enabled"] and settings["pin_hash"]):
        self._inactivity_watcher.suspend()

    def check_initial_lock(self):
      """Shows the lock screen at launch, if enabled. Must be called by
      main.py only AFTER the splash-to-window crossfade has fully
      finished and window.raise_()/activateWindow() have already run —
      not from inside __init__.

      Previously this ran via QTimer.singleShot(0, ...) fired from
      _setup_app_lock() during __init__. main.py's _begin_zoom()
      constructs the window, shows it at opacity 0, and starts a
      ~420ms splash<->window crossfade before returning control to the
      event loop — so that singleShot(0) fired almost immediately,
      opening the modal AppLockDialog while the crossfade was still in
      flight. ~420ms later, the crossfade's on_done callback called
      window.raise_() and window.activateWindow() on the *parent*
      window while the lock dialog was still exec()-ing underneath —
      which could reorder the frameless always-on-top lock dialog
      behind the newly-activated main window. The dialog would
      visually vanish ("pops up and goes off") while still being the
      modal loop blocking all input, leaving nothing on screen to
      interact with. Deferring this call until after the window is
      fully raised/activated removes the race entirely.
      """
      settings = security.get_lock_settings()
      if settings["enabled"] and settings["pin_hash"]:
        self._show_lock_screen()

    def _on_app_state_changed(self, state):
      # Fires when this app is brought back to the foreground (e.g. the
      # user alt-tabs/cmd-tabs back in). If an auto-lock fired while we
      # were in the background, show the lock screen now instead of
      # having already popped it up over another app.
      if state == Qt.ApplicationActive and self._lock_pending:
        self._lock_pending = False
        self._show_lock_screen()

    def _show_lock_screen(self):
      # Auto-lock can fire while a previous lock dialog is still being
      # torn down, or while the app lock got disabled mid-timeout —
      # guard against showing it twice or when there's nothing to check.
      settings = security.get_lock_settings()
      if self._lock_dlg_open or not settings["enabled"] or not settings["pin_hash"]:
        return

      # Don't steal focus from another app the user is actively using —
      # remember that a lock is owed and show it once they switch back.
      if QApplication.instance().applicationState() != Qt.ApplicationActive:
        self._lock_pending = True
        if self._inactivity_watcher:
          self._inactivity_watcher.suspend()
        return

      self._lock_dlg_open = True
      if self._inactivity_watcher:
        self._inactivity_watcher.suspend()
      try:
        dlg = AppLockDialog(self)
        dlg.exec_() # blocks until the correct PIN is entered, or the app quits
      finally:
        self._lock_dlg_open = False
        if self._inactivity_watcher and security.get_lock_settings()["enabled"]:
          self._inactivity_watcher.resume()

