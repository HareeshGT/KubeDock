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
from nav_rail import NavRail, RAIL_WIDTH
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


class UIMixin:
    def _build_ui(self):
      # ── Toolbar (plain QWidget — avoids QToolBar proxy glitches) ──
      tb_widget = QWidget()
      tb_widget.setFixedHeight(44)
      self._toolbar = tb_widget
      tb = QHBoxLayout(tb_widget)
      tb.setContentsMargins(8, 0, 8, 0)
      tb.setSpacing(4)

      def _tbtn(text, tooltip="", checkable=False, width=None):
        b = QPushButton()
        apply_text_icon(b, text)
        b.setToolTip(tooltip)
        b.setStyleSheet("padding: 4px 10px;")
        b.setFixedHeight(28)
        if checkable:
          b.setCheckable(True)
        if width:
          b.setFixedWidth(width)
        return b

      def _icon_btn(text, tooltip="", checkable=False, icon_name=None):
        """Compact square icon button.

        apply_text_icon() derives the SVG from the *label*, so a button that
        is meant to be icon-only (empty label) gets no icon at all and renders
        as an empty box. For those, resolve the icon from an explicit
        icon_name, falling back to the tooltip — and keep the label empty so
        the tooltip text never leaks into the 30x28 button face.
        """
        b = QPushButton()
        apply_text_icon(b, text)
        if not text.strip():
          name = icon_name or split_icon_text(tooltip)[0]
          if name:
            set_icon(b, name, size=16)
          b.setText("")
        b.setToolTip(tooltip)
        b.setFixedSize(30, 28)
        b.setStyleSheet("padding: 0; font-size: 14px;")
        if checkable:
          b.setCheckable(True)
        return b

      def _sep():
        f = QFrame()
        f.setFrameShape(QFrame.VLine)
        f.setFixedWidth(1)
        f.setFixedHeight(20)
        f.setStyleSheet(f"background: {T['BORDER']};")
        return f

      def _lbl(text):
        l = QLabel(text)
        l.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 13px;")
        return l

      # Nav buttons (FM-only)
      self.act_back  = _icon_btn("◀", "Back")
      self.act_forward = _icon_btn("▶", "Forward")
      self.act_up   = _icon_btn("↑", "Go up")
      self.act_refresh = _icon_btn("↺", "Refresh")

      self.act_back.clicked.connect(self._go_back)
      self.act_forward.clicked.connect(self._go_forward)
      self.act_up.clicked.connect(self._go_up)
      self.act_refresh.clicked.connect(self._refresh)

      self._fm_seps = []  # separators that only appear in FM mode

      for b in [self.act_back, self.act_forward, self.act_up, self.act_refresh]:
        tb.addWidget(b)
      sep1 = _sep(); tb.addWidget(sep1); self._fm_seps.append(sep1)

      self.addr_bar = QLineEdit()
      self.addr_bar.setPlaceholderText("Path…")
      self.addr_bar.returnPressed.connect(self._navigate_addr)
      tb.addWidget(self.addr_bar, 3)
      sep2 = _sep(); tb.addWidget(sep2); self._fm_seps.append(sep2)

      self.search_bar = QLineEdit()
      self.search_bar.setPlaceholderText("Filter…")
      self.search_bar.textChanged.connect(self._filter_list)
      self.search_bar.setFixedWidth(140)
      tb.addWidget(self.search_bar)
      sep3 = _sep(); tb.addWidget(sep3); self._fm_seps.append(sep3)

      self._sort_lbl = _lbl("Sort")
      tb.addWidget(self._sort_lbl)
      self.sort_combo = QComboBox()
      self.sort_combo.addItems(["Name", "Size", "Type"])
      self.sort_combo.setCurrentText(self.sort_key)
      self.sort_combo.currentTextChanged.connect(self._change_sort_key)
      self.sort_combo.setFixedWidth(80)
      tb.addWidget(self.sort_combo)

      self.sort_dir_btn = _icon_btn("↑", "Toggle sort direction")
      self.sort_dir_btn.clicked.connect(self._toggle_sort_direction)
      tb.addWidget(self.sort_dir_btn)
      sep4 = _sep(); tb.addWidget(sep4); self._fm_seps.append(sep4)

      self.view_list_btn = _icon_btn("", "List view", checkable=True, icon_name="list")
      self.view_list_btn.setChecked(True)
      self.view_list_btn.clicked.connect(lambda: self._set_view_mode("list"))
      tb.addWidget(self.view_list_btn)

      self.view_grid_btn = _icon_btn("", "Grid view", checkable=True, icon_name="grid")
      self.view_grid_btn.clicked.connect(lambda: self._set_view_mode("grid"))
      tb.addWidget(self.view_grid_btn)
      sep5 = _sep(); tb.addWidget(sep5); self._fm_seps.append(sep5)

      self.theme_picker = ThemePicker(_themes.CURRENT_THEME)
      self.theme_picker.theme_changed.connect(self._change_theme)
      tb.addWidget(self.theme_picker)

      self.act_settings = _icon_btn("", "Settings")
      self.act_settings.clicked.connect(self._open_settings)
      tb.addWidget(self.act_settings)

      tb.addStretch()

      # Must come *after* the stretch: placed before it, this separator stays
      # glued to the left-hand group, so on the Kubernetes/Dashboard tabs
      # (where the FM controls are hidden) it floats alone in empty space
      # a thousand pixels from the buttons it is supposed to separate.
      tb.addWidget(_sep()) # always-visible sep before connect buttons

      self.act_connect  = _tbtn(" Connect",  "Connect to server")
      self.act_disconnect = _tbtn(" Disconnect", "Disconnect")
      self.act_connect.clicked.connect(self._connect)
      # clicked emits a bool (the button's checked state) — connecting
      # directly to _disconnect(self, reason="Disconnected") lets that
      # bool clobber the `reason` default, which later crashes
      # self.status.showMessage(reason) with a TypeError (and, since
      # it's raised inside a Qt-invoked slot, PyQt5 aborts the process
      # instead of just printing a traceback). The lambda swallows the
      # bool so _disconnect() always gets its intended default.
      self.act_disconnect.clicked.connect(lambda: self._disconnect())
      tb.addWidget(self.act_connect)
      tb.addWidget(self.act_disconnect)

      self.switch_user_btn = _tbtn(" Switch User", "sudo -u <user> — switch context for terminal and file ops")
      self.switch_user_btn.clicked.connect(self._on_switch_user_btn)
      tb.addWidget(self.switch_user_btn)

      # Main tabs
      self.main_tabs = QTabWidget()
      self.main_tabs.setTabPosition(QTabWidget.North)
      self.main_tabs.currentChanged.connect(self._on_main_tab_change)

      # Root layout: left nav rail | (toolbar on top, pages below)
      self.main_tabs.tabBar().hide()   # navigation is driven by the NavRail
      self.nav_rail = NavRail()
      self.nav_rail.navigate.connect(self._on_nav_rail)
      self.sidebar = Sidebar()
      self.sidebar.navigate.connect(self._nav_to)
      self.sidebar.setFixedWidth(RAIL_WIDTH - 20)
      self.sidebar.set_embedded(True)
      self.nav_rail.add_quick_access(self.sidebar)

      right_widget = QWidget()
      right_layout = QVBoxLayout(right_widget)
      right_layout.setContentsMargins(0, 0, 0, 0)
      right_layout.setSpacing(0)
      right_layout.addWidget(tb_widget)
      right_layout.addWidget(self.main_tabs, 1)

      root_widget = QWidget()
      root_layout = QHBoxLayout(root_widget)
      root_layout.setContentsMargins(0, 0, 0, 0)
      root_layout.setSpacing(0)
      root_layout.addWidget(self.nav_rail)
      root_layout.addWidget(right_widget, 1)
      self.setCentralWidget(root_widget)

      # ── File manager tab ──────────────────────────────────
      fm_widget = QWidget()
      fm_root  = QVBoxLayout(fm_widget)
      fm_root.setContentsMargins(0, 0, 0, 0)
      fm_root.setSpacing(0)

      self.progress = QProgressBar()
      self.progress.setFixedHeight(4)
      self.progress.setRange(0, 0)
      self.progress.hide()
      fm_root.addWidget(self.progress)

      splitter = QSplitter(Qt.Horizontal)
      splitter.setHandleWidth(1)

      # Quick Access (self.sidebar) now lives in the left NavRail.

      list_col = QWidget()
      list_col.setStyleSheet("background: {};".format(T['BG_DARK']))
      list_layout = QVBoxLayout(list_col)
      list_layout.setContentsMargins(0, 0, 0, 0)
      list_layout.setSpacing(0)

      self.list_header = self._build_list_header()
      list_layout.addWidget(self.list_header)

      self.file_list = QListWidget()
      self.file_list.setMouseTracking(True)
      self.file_list.setSelectionMode(QAbstractItemView.SingleSelection)
      self.file_list.setSpacing(0)
      self.file_list.itemClicked.connect(self._on_click)
      self.file_list.itemDoubleClicked.connect(self._on_double_click)
      self.file_list.setContextMenuPolicy(Qt.CustomContextMenu)
      self.file_list.customContextMenuRequested.connect(self._ctx_menu)
      QShortcut(QKeySequence("F2"), self.file_list).activated.connect(self._rename_selected)
      list_layout.addWidget(self.file_list)

      # ── Row 1: file action buttons ────────────────────────
      act_bar = QWidget()
      act_bar.setFixedHeight(44)
      act_bar.setStyleSheet("background: {}; border-top: 1px solid {};".format(
        T['BG_PANEL'], T['BORDER']))
      act_layout = QHBoxLayout(act_bar)
      act_layout.setContentsMargins(8, 0, 8, 0)
      act_layout.setSpacing(6)

      for label, slot in [
        ("⬆ Upload",   self._upload),
        ("⬇ Download",  self._download),
        (" Edit",    self._edit_selected),
        ("▶ Run",    self._run_selected),
        (" Search",  self._open_search),
        (" New Folder", self._new_folder),
        ("⌨ Rename",   self._rename_selected),
        (" Delete",   self._delete),
      ]:
        btn = QPushButton(label)
        btn.setFixedSize(118, 32)
        btn.clicked.connect(slot)
        act_layout.addWidget(btn)
      act_layout.addStretch()
      list_layout.addWidget(act_bar)

      self._list_col = list_col
      self._act_bar = act_bar
      splitter.addWidget(list_col)

      # Right column: preview + terminal
      right_col = QSplitter(Qt.Vertical)
      right_col.setHandleWidth(1)

      self.preview = PreviewPane()
      right_col.addWidget(self.preview)

      terminal_widget = QWidget()
      terminal_widget.setStyleSheet("background: {};".format(T['BG_PANEL']))
      tl = QVBoxLayout(terminal_widget)
      tl.setContentsMargins(0, 0, 0, 0)
      tl.setSpacing(0)

      header_row = QHBoxLayout()
      header_row.setContentsMargins(0, 0, 0, 0)
      header_row.setSpacing(0)

      self.t_header = QLabel(" Terminal")
      self.t_header.setFixedHeight(28)
      header_row.addWidget(self.t_header, 1)

      self.terminal_explain_btn = QPushButton()
      set_icon(self.terminal_explain_btn, "explain", size=16)
      self.terminal_explain_btn.setToolTip("Explain the last failed command with AI")
      self.terminal_explain_btn.setFixedSize(28, 28)
      self.terminal_explain_btn.setStyleSheet("padding: 0px;")
      self.terminal_explain_btn.setEnabled(False)
      self.terminal_explain_btn.clicked.connect(self._on_terminal_explain)
      header_row.addWidget(self.terminal_explain_btn)

      self.terminal_popout_btn = QPushButton("⤢")
      self.terminal_popout_btn.setToolTip("Open terminal in its own window")
      self.terminal_popout_btn.setFixedSize(28, 28)
      self.terminal_popout_btn.setStyleSheet("padding: 0px;")
      self.terminal_popout_btn.clicked.connect(self._toggle_terminal_popout)
      header_row.addWidget(self.terminal_popout_btn)

      tl.addLayout(header_row)

      # Single scrolling surface — type directly at the prompt shown in the
      # same history as the output, instead of a separate input box above
      # a separate read-only output box.
      self.terminal = TerminalWidget()
      self.terminal.command_entered.connect(self._on_terminal_command)
      self.terminal.interrupt_requested.connect(self._on_terminal_interrupt)
      tl.addWidget(self.terminal)

      self.terminal_dock_placeholder = QLabel("Terminal is open in its own window.")
      self.terminal_dock_placeholder.setAlignment(Qt.AlignCenter)
      self.terminal_dock_placeholder.setStyleSheet(f"color: {T['TEXT_MUTED']}; padding: 24px;")
      self.terminal_dock_placeholder.hide()
      tl.addWidget(self.terminal_dock_placeholder)

      right_col.addWidget(terminal_widget)
      right_col.setSizes([320, 320])

      self._terminal_widget = terminal_widget
      self._terminal_home_layout = tl
      self._terminal_popout_win = None
      splitter.addWidget(right_col)
      splitter.setSizes([600, 300])
      fm_root.addWidget(splitter)

      add_icon_tab(self.main_tabs, fm_widget, " File Manager")

      # ── Kubernetes tab ────────────────────────────────────
      self.k8s_tab = KubernetesTab()
      self.k8s_tab.status_msg.connect(lambda m: self.status.showMessage(m))
      # Kubernetes gets the ⎈ helm-wheel glyph instead of the SVG icon set
      # used for every other tab — it's the symbol most people recognize
      # for k8s at a glance, so plain addTab() (not add_icon_tab) here.
      # Icon+text tabs (add_icon_tab) get an automatic icon-to-text gap
      # from Qt on top of the QSS side padding; a plain text tab like this
      # one doesn't, so the leading/trailing space below gives the glyph
      # and the word the same breathing room the other tabs get for free
      # — without it the label reads as pressed right up against the edges.
      self.main_tabs.addTab(self.k8s_tab, " ⎈  Kubernetes ")

      # ── Dashboard tab ──────────────────────────────────────
      self.dashboard_tab = DashboardTab()
      self.dashboard_tab.status_msg.connect(lambda m: self.status.showMessage(m))
      # Keep the Dashboard's node/pod snapshot pinned to whatever cluster
      # context is selected in the Kubernetes tab. Without this, DashboardTab
      # never calls set_kube_context() at all, so its kubectl calls carry no
      # --context flag and silently run against the SSH session's ambient
      # current-context instead — which can be a *different* cluster than the
      # one shown in the Kubernetes tab (e.g. across dev/test/prod EKS/AKS
      # clusters sharing one kubeconfig). That produces exactly the kind of
      # "node exists but its pods don't match" mismatch the Dashboard's node
      # detail window would otherwise show with no indication anything was
      # pointed at the wrong cluster.
      self.k8s_tab.context_changed.connect(self.dashboard_tab.set_kube_context)
      add_icon_tab(self.main_tabs, self.dashboard_tab, " Dashboard")

      # Land on the Dashboard tab by default rather than File Manager.
      self.main_tabs.setCurrentWidget(self.dashboard_tab)
      self.k8s_tab.sub_tabs.currentChanged.connect(lambda _i: self._sync_nav_rail())
      self._sync_nav_rail()

      # ── Status bar ────────────────────────────────────────
      self.status = QStatusBar()
      self.setStatusBar(self.status)
      self.conn_lbl = QLabel("Not connected")
      self.conn_lbl.setStyleSheet("color: {};".format(T['TEXT_MUTED']))
      self.status.addPermanentWidget(self.conn_lbl)

      self.sudo_badge = QLabel()
      self.sudo_badge.hide()
      self.status.addPermanentWidget(self.sudo_badge)

      self.status.showMessage("Ready")
      self._apply_inline_styles()

    def _apply_inline_styles(self):
      self._toolbar.setStyleSheet(
        "background: {}; border-bottom: 1px solid {};".format(T['BG_PANEL'], T['BORDER'])
      )
      self._list_col.setStyleSheet("background: {};".format(T['BG_DARK']))
      self._act_bar.setStyleSheet("background: {}; border-top: 1px solid {};".format(
        T['BG_PANEL'], T['BORDER']))
      self._terminal_widget.setStyleSheet("background: {};".format(T['BG_PANEL']))
      self.t_header.setStyleSheet(
        "background: {bg}; color: {fg}; "
        "border-top: 1px solid {b}; border-bottom: 1px solid {b}; "
        "font-size: 10px; font-weight: 700; letter-spacing: 1px; padding-left: 10px;".format(
          bg=T['BG_PANEL'], fg=T['TEXT_MUTED'], b=T['BORDER'])
      )
      self.terminal.setStyleSheet(
        "background: #0d0d1a; color: {}; border: none; padding: 8px;".format(T['SUCCESS'])
      )
      self.conn_lbl.setStyleSheet(
        "color: {};".format(T['SUCCESS'] if self.ssh else T['TEXT_MUTED'])
      )
      self._update_sudo_badge()

    def _build_list_header(self):
      COL_ICON = FileRowWidget.COL_ICON
      COL_SIZE = FileRowWidget.COL_SIZE
      COL_TYPE = FileRowWidget.COL_TYPE

      hdr = QWidget()
      hdr.setFixedHeight(28)
      hdr.setStyleSheet("background: {}; border-bottom: 1px solid {};".format(
        T['BG_PANEL'], T['BORDER']))

      layout = QHBoxLayout(hdr)
      layout.setContentsMargins(12, 0, 12, 0)
      layout.setSpacing(0)

      def _lbl(text, align=Qt.AlignLeft | Qt.AlignVCenter):
        l = QLabel(text)
        l.setStyleSheet(
          "color: {}; font-size: 13px; font-weight: 700; letter-spacing: 0.5px;".format(
            T['TEXT_MUTED'])
        )
        l.setAlignment(align)
        return l

      spacer = QWidget()
      spacer.setFixedWidth(COL_ICON)
      spacer.setStyleSheet("background: transparent;")
      layout.addWidget(spacer)
      layout.addWidget(_lbl("Name"), 1)

      size_lbl = _lbl("Size", Qt.AlignRight | Qt.AlignVCenter)
      size_lbl.setFixedWidth(COL_SIZE)
      layout.addWidget(size_lbl)
      layout.addSpacing(8)

      type_lbl = _lbl("Type")
      type_lbl.setFixedWidth(COL_TYPE)
      layout.addWidget(type_lbl)
      return hdr

    def _rebuild_list_header(self):
      old  = self.list_header
      new  = self._build_list_header()
      layout = self._list_col.layout()
      layout.replaceWidget(old, new)
      old.deleteLater()
      self.list_header = new
      if self.view_mode == "grid":
        self.list_header.hide()

    # ── Left NavRail → existing tabs/actions (no behavior change) ──
    def _on_nav_rail(self, key):
      if key == "dashboard":
        self.main_tabs.setCurrentWidget(self.dashboard_tab)
      elif key == "kubernetes":
        self.main_tabs.setCurrentWidget(self.k8s_tab)
      elif key == "files":
        self.main_tabs.setCurrentIndex(0)
      elif key == "terminal":
        self.main_tabs.setCurrentIndex(0)   # terminal pane lives beside the file list
        t = getattr(self, "terminal", None)
        if t is not None:
          t.setFocus()
      elif key == "tunnels":
        self.main_tabs.setCurrentWidget(self.k8s_tab)
        subs = self.k8s_tab.sub_tabs
        for i in range(subs.count()):
          if "Tunnels" in subs.tabText(i):
            subs.setCurrentIndex(i)
            break
      elif key == "ec2":
        self._connect()      # same Connect dialog as the toolbar button
      self._sync_nav_rail()

    def _sync_nav_rail(self):
      """Highlight the rail item matching the visible page."""
      if not hasattr(self, "nav_rail") or not hasattr(self, "dashboard_tab"):
        return
      w = self.main_tabs.currentWidget()
      if w is self.dashboard_tab:
        key = "dashboard"
      elif w is self.k8s_tab:
        subs = self.k8s_tab.sub_tabs
        key = "tunnels" if "Tunnels" in subs.tabText(subs.currentIndex()) else "kubernetes"
      else:
        key = "files"
      self.nav_rail.set_active(key)
      # Quick Access only makes sense for the file manager.
      self.sidebar.setVisible(key == "files")

    def _on_main_tab_change(self, idx):
      new_widget = self.main_tabs.widget(idx)
      if (self._tab_slide_ready and new_widget is not None
          and self._current_tab_widget is not None
          and new_widget is not self._current_tab_widget):
        direction = 1 if idx > self._current_tab_idx else -1
        self._animate_tab_slide(self._current_tab_widget, new_widget, direction)
      self._current_tab_widget = new_widget
      self._current_tab_idx  = idx
      self._sync_nav_rail()

      fm_mode = (idx == 0)
      for b in [self.act_back, self.act_forward, self.act_up, self.act_refresh]:
        b.setVisible(fm_mode)
      self.addr_bar.setVisible(fm_mode)
      self.search_bar.setVisible(fm_mode)
      self._sort_lbl.setVisible(fm_mode)
      self.sort_combo.setVisible(fm_mode)
      self.sort_dir_btn.setVisible(fm_mode)
      self.view_list_btn.setVisible(fm_mode)
      self.view_grid_btn.setVisible(fm_mode)
      for sep in self._fm_seps:
        sep.setVisible(fm_mode)
      # Dashboard only polls while it's the visible tab — this is what
      # puts it to sleep the instant the user navigates elsewhere.
      # Guarded: QTabWidget emits currentChanged as soon as the first tab
      # is added (index -1 -> 0), which happens earlier in _build_ui than
      # dashboard_tab is constructed — so this can fire before the
      # attribute exists.
      if hasattr(self, "dashboard_tab"):
        self.dashboard_tab.set_active(idx == self.main_tabs.indexOf(self.dashboard_tab))

    def _animate_tab_slide(self, old_widget, new_widget, direction):
      # A rapid second tab click while one slide is still playing:
      # finish/clean up the first instead of stacking overlays.
      self._finish_tab_slide()

      tab_bar = self.main_tabs.tabBar()
      tab_bar_h = tab_bar.height() if (tab_bar and tab_bar.isVisible()) else 0
      rect = QRect(0, tab_bar_h, self.main_tabs.width(),
             max(0, self.main_tabs.height() - tab_bar_h))
      if rect.width() <= 0 or rect.height() <= 0:
        return # window not laid out yet (e.g. still hidden) — nothing to animate

      old_pix = old_widget.grab()
      new_pix = new_widget.grab()
      if old_pix.isNull() or new_pix.isNull():
        return

      offset = QPoint(rect.width() * direction, 0)

      lbl_old = QLabel(self.main_tabs)
      lbl_old.setPixmap(old_pix)
      lbl_old.setGeometry(rect)
      lbl_old.show()
      lbl_old.raise_()

      lbl_new = QLabel(self.main_tabs)
      lbl_new.setPixmap(new_pix)
      lbl_new.setGeometry(rect.translated(offset))
      lbl_new.show()
      lbl_new.raise_()

      self._tab_anim_overlays = [lbl_old, lbl_new]

      group = QParallelAnimationGroup(self)
      for lbl, start_rect, end_rect in (
        (lbl_old, rect, rect.translated(-offset.x(), -offset.y())),
        (lbl_new, rect.translated(offset), rect),
      ):
        anim = QPropertyAnimation(lbl, b"geometry", self)
        anim.setDuration(240)
        anim.setStartValue(start_rect)
        anim.setEndValue(end_rect)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        group.addAnimation(anim)

      group.finished.connect(self._finish_tab_slide)
      self._tab_anim_group = group
      group.start()

    def _finish_tab_slide(self):
      if self._tab_anim_group is not None:
        self._tab_anim_group.stop()
        self._tab_anim_group = None
      for lbl in self._tab_anim_overlays:
        lbl.deleteLater()
      self._tab_anim_overlays = []

    def _set_connected(self, ok):
      for b in [self.act_back, self.act_forward, self.act_up, self.act_refresh]:
        b.setEnabled(ok)
      self.act_connect.setEnabled(not ok)
      self.act_disconnect.setEnabled(ok)
      if ok:
        self.conn_lbl.setText("Connected — {}".format(self.host_label))
        self.conn_lbl.setStyleSheet("color: {};".format(T['SUCCESS']))
      else:
        self.conn_lbl.setText("Not connected")
        self.conn_lbl.setStyleSheet("color: {};".format(T['TEXT_MUTED']))

