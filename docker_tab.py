"""Docker management tab.

Talks to the Docker CLI on the connected SSH host (the same host the
Kubernetes tab uses), so no extra connection or credentials are involved.
Command construction and output parsing live in docker_utils.py.
"""

import json

from PyQt5.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
  QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QInputDialog,
  QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
  QSizePolicy, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

import docker_utils as du
from docker_dialogs import DockerLogDialog, DockerTextDialog
from themes import T
from ui_kit import ResourceCard, pill_qss, RADIUS_CARD
from utils import monospace_font
from ui_icons import set_icon
from workers import CommandWorker, track_worker

_KIND_TITLES = {
  "containers": "Containers",
  "images": "Images",
  "volumes": "Volumes",
  "networks": "Networks",
}
# The column holding the value used to identify a row in commands.
_IDENT_KEY = {
  "containers": "Names",
  "images": "ID",
  "volumes": "Name",
  "networks": "Name",
}
_AUTO_REFRESH_MS = 8000


class DockerResourceCard(ResourceCard):
  """Themed card for one Docker resource; actions are delegated to DockerTab."""

  OBJ = "docker_resource_card"
  clicked = pyqtSignal()
  double_clicked = pyqtSignal()
  action_requested = pyqtSignal(str)

  def __init__(self, kind, row, parent=None):
    self.kind = kind
    self.row = {}
    self._full_title = ""
    super().__init__(parent)
    self.setMinimumSize(330, 202)
    self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    outer = QVBoxLayout(self)
    outer.setContentsMargins(12, 10, 12, 10)
    outer.setSpacing(7)
    header = QHBoxLayout()
    header.setSpacing(8)

    self.type_badge = QLabel()
    self.type_badge.setFixedSize(28, 28)
    self.type_badge.setAlignment(Qt.AlignCenter)
    header.addWidget(self.type_badge)
    self.title_lbl = QLabel()
    self.title_lbl.setFont(monospace_font(13, bold=True))
    self.title_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    self.title_lbl.setWordWrap(False)
    self.title_lbl.setMinimumWidth(80)
    header.addWidget(self.title_lbl, 1)
    self.state_badge = QLabel()
    self.state_badge.setAlignment(Qt.AlignCenter)
    header.addWidget(self.state_badge)
    outer.addLayout(header)

    self.detail_labels = []
    for _ in range(4):
      label = QLabel()
      label.setWordWrap(False)
      label.setTextInteractionFlags(Qt.NoTextInteraction)
      label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
      label.setFixedHeight(17)
      label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
      outer.addWidget(label)
      self.detail_labels.append(label)
    outer.addStretch(1)

    actions = QHBoxLayout()
    actions.setSpacing(5)
    self.action_buttons = {}
    specs = {
      "containers": [("start", "Start"), ("stop", "Stop"), ("logs", "Logs"), ("inspect", "Inspect")],
      "images": [("inspect", "Inspect"), ("remove", "Remove")],
      "volumes": [("inspect", "Inspect"), ("remove", "Remove")],
      "networks": [("inspect", "Inspect"), ("remove", "Remove")],
    }
    for action, caption in specs[kind]:
      button = QPushButton(caption)
      button.setCursor(Qt.PointingHandCursor)
      button.setFixedHeight(25)
      button.clicked.connect(lambda _checked=False, a=action: self.action_requested.emit(a))
      self.action_buttons[action] = button
      actions.addWidget(button)
    actions.addStretch(1)
    outer.addLayout(actions)

    for label in (self.title_lbl, self.type_badge, self.state_badge):
      label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    self.update_row(row)
    self.refresh_theme()

  def _title(self, row):
    if self.kind == "images":
      return du.image_display_ref(row)
    if self.kind == "containers":
      return du.cell(row, "Names") or du.cell(row, "ID") or "Unnamed container"
    return du.cell(row, "Name") or du.cell(row, "ID") or "Unnamed {}".format(self.kind[:-1])

  def _details(self, row):
    if self.kind == "containers":
      result = [
        "Image  ·  {}".format(du.cell(row, "Image") or "Unavailable"),
        "ID  ·  {}".format((du.cell(row, "ID") or "Unavailable")[:12]),
        "Ports  ·  {}".format(du.cell(row, "Ports") or "No published ports"),
        "Uptime  ·  {}".format(du.cell(row, "Status") or "Unavailable"),
      ]
      cpu = du.cell(row, "CPUPerc") or du.cell(row, "CPU")
      mem = du.cell(row, "MemUsage") or du.cell(row, "Memory")
      mem_pct = du.cell(row, "MemPerc")
      usage = []
      if cpu:
        usage.append("CPU {}".format(cpu))
      if mem:
        usage.append("Memory {}".format(mem))
      if mem_pct:
        usage.append("Mem {}".format(mem_pct))
      if usage:
        result[-1] = "Uptime  ·  {}  ·  {}".format(
          du.cell(row, "Status") or "Unavailable", "  ·  ".join(usage))
      return result
    if self.kind == "images":
      return [
        "ID  ·  {}".format((du.cell(row, "ID") or "Unavailable")[:19]),
        "Size  ·  {}".format(du.cell(row, "Size") or "Unavailable"),
        "Created  ·  {}".format(du.cell(row, "CreatedSince") or du.cell(row, "CreatedAt") or "Unavailable"),
        "Repository  ·  {}".format(du.cell(row, "Repository") or "Dangling image"),
      ]
    if self.kind == "volumes":
      mount = du.cell(row, "Mountpoint") or du.cell(row, "Mount Point") or du.cell(row, "MountpointPath")
      return [
        "Driver  ·  {}".format(du.cell(row, "Driver") or "Unavailable"),
        "Scope  ·  {}".format(du.cell(row, "Scope") or "Unavailable"),
        "Mount  ·  {}".format(mount or "Not provided by list metadata"),
        "Status  ·  {}".format(du.cell(row, "Status") or "Not provided by list metadata"),
      ]
    connected = row.get("Containers", row.get("ContainerCount", row.get("ContainersCount")))
    if isinstance(connected, (list, tuple, dict)):
      connected = len(connected)
    count = str(connected) if connected is not None and str(connected) else "Not provided by list metadata"
    return [
      "Driver  ·  {}".format(du.cell(row, "Driver") or "Unavailable"),
      "Scope  ·  {}".format(du.cell(row, "Scope") or "Unavailable"),
      "Connected containers  ·  {}".format(count),
      "ID  ·  {}".format((du.cell(row, "ID") or "Unavailable")[:19]),
    ]

  def update_row(self, row):
    self.row = dict(row or {})
    self._full_title = self._title(self.row) or "Unnamed resource"
    self.title_lbl.setToolTip(self._full_title)
    self._set_elided_title()
    for label, text in zip(self.detail_labels, self._details(self.row)):
      label.setText(text)
      label.setToolTip(text)
    if self.kind == "containers":
      state = du.cell(self.row, "State") or "unknown"
      category = du.state_kind(state)
      color = {
        "running": T["SUCCESS"], "paused": T["WARNING"],
        "stopped": T["TEXT_MUTED"], "other": T["TEXT_DIM"],
      }.get(category, T["TEXT_DIM"])
      self.state_badge.setText(state.capitalize())
      self.state_badge.setStyleSheet(pill_qss(color))
      self.action_buttons["start"].setEnabled(category == "stopped")
      self.action_buttons["stop"].setEnabled(category in ("running", "paused"))
      self.action_buttons["logs"].setEnabled(category in ("running", "paused", "stopped"))
    else:
      self.state_badge.setText(self.kind[:-1].capitalize())
      self.state_badge.setStyleSheet(pill_qss(T["ACCENT"]))
    self._style_children()

  def _set_elided_title(self):
    if hasattr(self, "title_lbl"):
      self.title_lbl.setText(
        self.title_lbl.fontMetrics().elidedText(
          self._full_title, Qt.ElideMiddle, max(80, self.title_lbl.width())))

  def resizeEvent(self, event):
    super().resizeEvent(event)
    self._set_elided_title()

  def set_selected(self, selected):
    selected = bool(selected)
    if selected != self._selected:
      self._selected = selected
      self.refresh_theme()

  def _style_children(self):
    if not hasattr(self, "title_lbl"):
      return
    self.title_lbl.setStyleSheet("color: {}; background: transparent;".format(T["TEXT_PRIMARY"]))
    for label in self.detail_labels:
      label.setStyleSheet("color: {}; font-size: 11px; background: transparent;".format(T["TEXT_DIM"]))
    accent = T["ACCENT"]
    glyph = {"containers": "▣", "images": "◈", "volumes": "◫", "networks": "⌘"}.get(self.kind, "•")
    self.type_badge.setText(glyph)
    self.type_badge.setStyleSheet(
      "color: {}; background: {}; border-radius: {}px; font-size: 16px; font-weight: 700;".format(
        accent, T["BG_DARK"], RADIUS_CARD))
    if self.kind == "containers":
      category = du.state_kind(du.cell(self.row, "State"))
      state_color = {
        "running": T["SUCCESS"], "paused": T["WARNING"],
        "stopped": T["TEXT_MUTED"], "other": T["TEXT_DIM"],
      }.get(category, T["TEXT_DIM"])
      self.state_badge.setStyleSheet(pill_qss(state_color))
    else:
      self.state_badge.setStyleSheet(pill_qss(T["ACCENT"]))
    for action, button in self.action_buttons.items():
      color = T["DANGER"] if action == "remove" else accent
      button.setStyleSheet(
        "QPushButton {{ color: {}; background: {}; border: 1px solid {}; "
        "border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: 600; }}"
        "QPushButton:hover {{ background: {}; }}"
        "QPushButton:disabled {{ color: {}; border-color: {}; background: transparent; }}".format(
          color, T["BG_PANEL"], T["BORDER"], T["BG_HOVER"], T["TEXT_MUTED"], T["BORDER"]))

  def mousePressEvent(self, event):
    if event.button() == Qt.LeftButton:
      self.clicked.emit()
    super().mousePressEvent(event)

  def mouseDoubleClickEvent(self, event):
    if event.button() == Qt.LeftButton:
      self.double_clicked.emit()
    super().mouseDoubleClickEvent(event)


class DockerTab(QWidget):
  status_msg = pyqtSignal(str)

  def __init__(self, parent=None):
    super().__init__(parent)
    self.ssh = None
    self._connection_generation = 0
    self._sudo_user = None
    self._workers = []
    self._inflight = set()
    self._stats_inflight = False
    self._details_inflight = set()
    self._dirty = False
    self._tables = {}       # existing selection/data model for Docker operations
    self._views = {}        # visible responsive card grids
    self._filters = {}
    self._empty_labels = {}
    self._buttons = {}      # (kind, key) -> QPushButton
    self._dialogs = []      # keep non-modal dialogs alive
    self._timer = QTimer(self)
    self._timer.timeout.connect(self._auto_refresh)
    self._build_ui()
    self._set_banner("Connect to a VM to manage its Docker containers.", error=False)

  # ── public API (mirrors KubernetesTab) ─────────────────────
  def set_ssh(self, ssh):
    self.ssh = ssh
    self._connection_generation += 1
    self._stats_inflight = False
    self._details_inflight.clear()
    self._clear_tables()
    if ssh is None:
      self._timer.stop()
      self._dirty = False
      self._set_banner("Connect to a VM to manage its Docker containers.", error=False)
      return
    self._set_banner("", error=False)
    self._dirty = True
    if self.isVisible():
      self.refresh_all()

  def set_sudo_user(self, username):
    self._sudo_user = username
    if self.ssh is not None:
      self._dirty = True
      if self.isVisible():
        self.refresh_all()

  def apply_theme(self):
    self._restyle()

  def refresh_all(self):
    self._dirty = False
    for kind in du.LIST_COMMANDS:
      self.refresh(kind)

  # ── UI ─────────────────────────────────────────────────────
  def _build_ui(self):
    root = QVBoxLayout(self)
    root.setContentsMargins(14, 12, 14, 10)
    root.setSpacing(8)

    top = QHBoxLayout()
    self.title = QLabel("Docker")
    top.addWidget(self.title)
    top.addStretch()
    self.auto_chk = QCheckBox("Auto-refresh")
    self.auto_chk.toggled.connect(self._on_auto_toggled)
    top.addWidget(self.auto_chk)
    self.refresh_btn = QPushButton(" Refresh")
    self.refresh_btn.clicked.connect(self.refresh_all)
    top.addWidget(self.refresh_btn)
    root.addLayout(top)

    self.banner = QLabel("")
    self.banner.setWordWrap(True)
    root.addWidget(self.banner)

    self.sub_tabs = QTabWidget()
    for kind in du.LIST_COMMANDS:
      self.sub_tabs.addTab(self._build_page(kind), _KIND_TITLES[kind])
    root.addWidget(self.sub_tabs, 1)
    self._restyle()

  def _build_page(self, kind):
    page = QWidget()
    lay = QVBoxLayout(page)
    lay.setContentsMargins(0, 8, 0, 0)
    lay.setSpacing(6)

    bar = QHBoxLayout()
    flt = QLineEdit()
    flt.setPlaceholderText("Filter {}…".format(_KIND_TITLES[kind].lower()))
    flt.setClearButtonEnabled(True)
    flt.setFixedWidth(240)
    flt.textChanged.connect(lambda _t, k=kind: self._apply_filter(k))
    self._filters[kind] = flt
    bar.addWidget(flt)
    bar.addStretch()

    def add_btn(key, label, handler, tip=""):
      b = QPushButton(label)
      if tip:
        b.setToolTip(tip)
      b.clicked.connect(lambda _=False, k=kind, h=handler: h(k))
      b.setEnabled(False)
      self._buttons[(kind, key)] = b
      bar.addWidget(b)
      return b

    if kind == "containers":
      add_btn("start", "Start", lambda k: self._container_action("start"))
      add_btn("stop", "Stop", lambda k: self._container_action("stop"))
      add_btn("restart", "Restart", lambda k: self._container_action("restart"))
      add_btn("pause", "Pause", lambda k: self._container_action("pause"))
      add_btn("unpause", "Unpause", lambda k: self._container_action("unpause"))
      add_btn("logs", "Logs", lambda k: self._show_logs())
      add_btn("exec", "Run command…", lambda k: self._run_in_container(),
              "Run one non-interactive command inside the container")
      add_btn("inspect", "Inspect", self._inspect)
      add_btn("remove", "Remove", self._remove)
    elif kind == "images":
      pull = QPushButton("Pull…")
      pull.clicked.connect(lambda _=False: self._pull_image())
      self._buttons[(kind, "pull")] = pull
      bar.addWidget(pull)
      add_btn("inspect", "Inspect", self._inspect)
      add_btn("remove", "Remove", self._remove)
    else:
      add_btn("inspect", "Inspect", self._inspect)
      add_btn("remove", "Remove", self._remove)
    lay.addLayout(bar)

    cols = du.COLUMNS[kind]
    # Retain the original table as a hidden selection/data model so existing
    # operation handlers and callers continue to use the same data semantics.
    table = QTableWidget(0, len(cols), page)
    table.setHorizontalHeaderLabels([c[0] for c in cols])
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.setShowGrid(False)
    table.setAlternatingRowColors(False)
    hdr = table.horizontalHeader()
    hdr.setSectionResizeMode(QHeaderView.Interactive)
    hdr.setStretchLastSection(True)
    table.itemSelectionChanged.connect(lambda k=kind: self._update_buttons(k))
    table.itemDoubleClicked.connect(lambda _i, k=kind: self._on_double_click(k))
    table.hide()
    self._tables[kind] = table

    view = QListWidget()
    view.setObjectName("docker_card_grid")
    view.setViewMode(QListView.IconMode)
    view.setFlow(QListView.LeftToRight)
    view.setWrapping(True)
    view.setResizeMode(QListView.Adjust)
    view.setMovement(QListView.Static)
    view.setSelectionMode(QAbstractItemView.SingleSelection)
    view.setSpacing(10)
    view.setGridSize(QSize(370, 218))
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
    view.setUniformItemSizes(True)
    view.itemClicked.connect(lambda item, k=kind: self._select_card_item(k, item))
    view.currentItemChanged.connect(
      lambda current, previous, k=kind: self._sync_card_selection(k, current, previous))
    self._views[kind] = view
    lay.addWidget(view, 1)

    empty = QLabel("Loading {}…".format(_KIND_TITLES[kind].lower()))
    empty.setAlignment(Qt.AlignCenter)
    empty.setWordWrap(True)
    empty.setStyleSheet("color: {}; font-size: 12px; padding: 24px;".format(T["TEXT_DIM"]))
    empty.hide()
    self._empty_labels[kind] = empty
    lay.addWidget(empty, 1)
    return page

  def _restyle(self):
    self.title.setStyleSheet(
      "color: {}; font-size: 16px; font-weight: 700;".format(T["TEXT_PRIMARY"]))
    set_icon(self.refresh_btn, "refresh", T["TEXT_DIM"], 14)
    self.auto_chk.setStyleSheet("color: {};".format(T["TEXT_DIM"]))
    self._style_banner(self._banner_error)
    for view in self._views.values():
      view.setStyleSheet(
        "QListWidget#docker_card_grid {{ background: {}; border: none; outline: none; }}"
        "QListWidget#docker_card_grid::item {{ background: transparent; border: none; }}"
        .format(T["BG_DARK"]))
      for index in range(view.count()):
        card = view.itemWidget(view.item(index))
        if card is not None:
          card.refresh_theme()

  _banner_error = False

  def _style_banner(self, error):
    color = T["DANGER"] if error else T["TEXT_DIM"]
    self.banner.setStyleSheet("color: {}; font-size: 12px; padding: 2px 0;".format(color))

  def _set_banner(self, text, error=True):
    self._banner_error = error
    self._style_banner(error)
    self.banner.setText(text)
    self.banner.setVisible(bool(text))

  # ── visibility / auto refresh ──────────────────────────────
  def showEvent(self, event):
    super().showEvent(event)
    if self.ssh is not None and self._dirty:
      self.refresh_all()
    if self.auto_chk.isChecked() and self.ssh is not None:
      self._timer.start(_AUTO_REFRESH_MS)

  def hideEvent(self, event):
    super().hideEvent(event)
    self._timer.stop()

  def _on_auto_toggled(self, on):
    if on and self.ssh is not None and self.isVisible():
      self._timer.start(_AUTO_REFRESH_MS)
    else:
      self._timer.stop()

  def _auto_refresh(self):
    if self.ssh is not None and self.isVisible():
      self.refresh_all()

  # ── running remote commands ────────────────────────────────
  def _worker(self, cmd, timeout=None):
    w = CommandWorker(self.ssh, cmd, sudo_user=self._sudo_user, timeout=timeout)
    track_worker(self._workers, w)
    return w

  def refresh(self, kind):
    if self.ssh is None or kind in self._inflight:
      return
    self._inflight.add(kind)
    self._apply_filter(kind)
    w = self._worker(du.list_command(kind))
    w.result.connect(lambda out, err, code, k=kind: self._on_listed(k, out, err, code))
    w.error.connect(lambda msg, k=kind: self._on_list_error(k, msg))
    w.start()

  def _on_listed(self, kind, out, err, code):
    self._inflight.discard(kind)
    if self.ssh is None:
      self._apply_filter(kind)
      return
    if code != 0:
      self._apply_filter(kind)
      if self._tables[kind].rowCount() == 0:
        self._empty_labels[kind].setText("Could not load {}.".format(_KIND_TITLES[kind].lower()))
      friendly = du.classify_error(err, code)
      first = (err or "").strip().splitlines()[0] if (err or "").strip() else ""
      self._set_banner(friendly or first or "Docker command failed (exit {}).".format(code))
      return
    self._set_banner("")
    rows = du.parse_json_lines(out)
    self._populate(kind, rows)
    if kind == "containers":
      self._refresh_container_stats()
    elif kind in ("volumes", "networks"):
      self._refresh_resource_details(kind)

  def _refresh_container_stats(self):
    """Fetch all running-container metrics in one worker, never per card."""
    if self.ssh is None or self._stats_inflight:
      return
    self._stats_inflight = True
    worker = self._worker(du.stats_command(), timeout=30)
    generation = self._connection_generation
    worker.result.connect(lambda out, err, code, g=generation: self._on_container_stats(out, err, code, g))
    worker.error.connect(lambda message, g=generation: self._on_container_stats_error(message, g))
    worker.start()

  def _on_container_stats(self, out, err, code, generation=None):
    if generation is not None and generation != self._connection_generation:
      return
    self._stats_inflight = False
    # Stats are optional metadata; a failure must not hide the real container list.
    if self.ssh is None or code != 0:
      return
    stats = du.parse_json_lines(out)
    if not stats:
      return

    by_ref = {}
    for item in stats:
      for key in ("ID", "Container", "Name"):
        value = du.cell(item, key)
        if value:
          by_ref[value] = item
          if key in ("ID", "Container"):
            by_ref[value[:12]] = item

    table = self._tables["containers"]
    rows = []
    for index in range(table.rowCount()):
      cell = table.item(index, 0)
      row = dict(cell.data(Qt.UserRole) or {}) if cell is not None else {}
      name = du.cell(row, "Names")
      ident = du.cell(row, "ID")
      metric = by_ref.get(ident) or by_ref.get(ident[:12]) or by_ref.get(name)
      if metric:
        for key in ("CPUPerc", "MemUsage", "MemPerc", "NetIO", "BlockIO", "PIDs"):
          value = du.cell(metric, key)
          if value:
            row[key] = value
      rows.append(row)
    self._populate("containers", rows)

  def _on_container_stats_error(self, _message, generation=None):
    if generation is not None and generation != self._connection_generation:
      return
    self._stats_inflight = False

  def _refresh_resource_details(self, kind):
    """Enrich volume/network cards with one bulk inspect, not per-card calls."""
    if self.ssh is None or kind in self._details_inflight:
      return
    self._details_inflight.add(kind)
    generation = self._connection_generation
    worker = self._worker(du.details_command(kind), timeout=30)
    worker.result.connect(
      lambda out, err, code, k=kind, g=generation: self._on_resource_details(k, out, err, code, g))
    worker.error.connect(
      lambda _message, k=kind, g=generation: self._on_resource_details_error(k, g))
    worker.start()

  def _on_resource_details(self, kind, out, err, code, generation=None):
    if generation is not None and generation != self._connection_generation:
      return
    self._details_inflight.discard(kind)
    # Listing data remains useful even if inspect is unsupported or fails.
    if self.ssh is None or code != 0:
      return
    details = du.parse_json_lines(out)
    if not details:
      return
    by_name = {du.cell(item, "Name"): item for item in details if du.cell(item, "Name")}
    table = self._tables[kind]
    rows = []
    for index in range(table.rowCount()):
      cell = table.item(index, 0)
      row = dict(cell.data(Qt.UserRole) or {}) if cell is not None else {}
      detail = by_name.get(du.cell(row, "Name"))
      if detail:
        row.update(detail)
      rows.append(row)
    self._populate(kind, rows)

  def _on_resource_details_error(self, kind, generation=None):
    if generation is not None and generation != self._connection_generation:
      return
    self._details_inflight.discard(kind)

  def _on_list_error(self, kind, msg):
    self._inflight.discard(kind)
    self._apply_filter(kind)
    if self._tables[kind].rowCount() == 0:
      self._empty_labels[kind].setText("Could not load {}.".format(_KIND_TITLES[kind].lower()))
    self._set_banner("Could not run docker: {}".format(msg))

  # ── table population ───────────────────────────────────────
  def _clear_tables(self):
    for table in self._tables.values():
      table.setRowCount(0)
      table.clearSelection()
    for view in self._views.values():
      view.clear()
    for kind in self._tables:
      self._update_buttons(kind)
      self._apply_filter(kind)

  def _populate(self, kind, rows):
    table = self._tables[kind]
    cols = du.COLUMNS[kind]
    keep = self._selected_ident(kind)
    was_empty = table.rowCount() == 0
    table.setUpdatesEnabled(False)
    table.setRowCount(len(rows))
    reselect = -1
    for r, row in enumerate(rows):
      for c, (_hdr, key) in enumerate(cols):
        item = QTableWidgetItem(du.cell(row, key))
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        if c == 0:
          item.setData(Qt.UserRole, row)
        if kind == "containers" and key == "State":
          item.setForeground(QColor(self._state_color(du.cell(row, "State"))))
        table.setItem(r, c, item)
      if keep and du.cell(row, _IDENT_KEY[kind]) == keep:
        reselect = r
    if was_empty and rows:
      # Size columns once, on first data; later refreshes keep the user's widths.
      table.resizeColumnsToContents()
      for c in range(table.columnCount()):
        table.setColumnWidth(c, min(table.columnWidth(c) + 16, 380))
    table.setUpdatesEnabled(True)
    self._populate_cards(kind, rows)
    if reselect >= 0:
      table.selectRow(reselect)
      self._set_card_current_row(kind, reselect)
    else:
      table.clearSelection()
      self._views[kind].setCurrentRow(-1)
    self._apply_filter(kind)
    self._update_buttons(kind)

  @staticmethod
  def _card_key(kind, row):
    if kind == "images":
      return "|".join(du.cell(row, key) for key in ("ID", "Repository", "Tag"))
    return du.cell(row, _IDENT_KEY[kind]) or du.cell(row, "ID")

  def _populate_cards(self, kind, rows):
    view = self._views[kind]
    keys = [self._card_key(kind, row) for row in rows]
    current_keys = [view.item(i).data(Qt.UserRole + 2) for i in range(view.count())]
    view.setUpdatesEnabled(False)
    # Normal refreshes update existing cards in place instead of recreating them.
    if keys == current_keys:
      for index, row in enumerate(rows):
        item = view.item(index)
        item.setData(Qt.UserRole, row)
        item.setData(Qt.UserRole + 1, index)
        card = view.itemWidget(item)
        if card is not None and card.row != row:
          card.update_row(row)
    else:
      view.clear()
      for index, row in enumerate(rows):
        item = QListWidgetItem()
        item.setData(Qt.UserRole, row)
        item.setData(Qt.UserRole + 1, index)
        item.setData(Qt.UserRole + 2, keys[index])
        item.setSizeHint(QSize(356, 204))
        view.addItem(item)
        card = DockerResourceCard(kind, row, view)
        card.clicked.connect(lambda k=kind, c=card: self._select_card_widget(k, c))
        card.double_clicked.connect(lambda k=kind, c=card: self._card_action(k, c, "inspect"))
        card.action_requested.connect(lambda action, k=kind, c=card: self._card_action(k, c, action))
        view.setItemWidget(item, card)
    view.setUpdatesEnabled(True)
    self._apply_filter(kind)

  def _select_card_item(self, kind, item):
    if item is None:
      return
    row_index = item.data(Qt.UserRole + 1)
    if row_index is None:
      return
    self._tables[kind].selectRow(int(row_index))
    self._sync_card_selection(kind, item, None)

  def _select_card_widget(self, kind, card):
    view = self._views[kind]
    for index in range(view.count()):
      item = view.item(index)
      if view.itemWidget(item) is card:
        self._select_card_item(kind, item)
        view.setCurrentItem(item)
        return

  def _set_card_current_row(self, kind, row_index):
    view = self._views[kind]
    if 0 <= row_index < view.count():
      view.setCurrentItem(view.item(row_index))
    else:
      view.setCurrentRow(-1)

  def _sync_card_selection(self, kind, current, previous):
    view = self._views[kind]
    for index in range(view.count()):
      item = view.item(index)
      card = view.itemWidget(item)
      if card is not None:
        card.set_selected(item is current)
    if current is None:
      self._tables[kind].clearSelection()
    else:
      row_index = current.data(Qt.UserRole + 1)
      if row_index is not None:
        table = self._tables[kind]
        selected = table.selectionModel().selectedRows()
        if not selected or selected[0].row() != int(row_index):
          table.selectRow(int(row_index))

  def _card_action(self, kind, card, action):
    self._select_card_widget(kind, card)
    if kind == "containers" and action in ("start", "stop"):
      self._container_action(action)
    elif kind == "containers" and action == "logs":
      self._show_logs()
    elif action == "inspect":
      self._inspect(kind)
    elif action == "remove":
      self._remove(kind)

  @staticmethod
  def _state_color(state):
    kind = du.state_kind(state)
    return {
      "running": T["SUCCESS"], "paused": T["WARNING"],
      "stopped": T["TEXT_MUTED"],
    }.get(kind, T["TEXT_DIM"])

  def _apply_filter(self, kind):
    needle = self._filters[kind].text().strip().lower()
    table = self._tables[kind]
    for r in range(table.rowCount()):
      text = " ".join(
        (table.item(r, c).text() if table.item(r, c) else "")
        for c in range(table.columnCount())
      ).lower()
      table.setRowHidden(r, bool(needle) and needle not in text)

    view = self._views.get(kind)
    empty = self._empty_labels.get(kind)
    if view is None or empty is None:
      return
    visible_count = 0
    for index in range(view.count()):
      item = view.item(index)
      row = item.data(Qt.UserRole) or {}
      searchable = " ".join(du.cell(row, key) for _header, key in du.COLUMNS[kind]).lower()
      hidden = bool(needle) and needle not in searchable
      item.setHidden(hidden)
      if not hidden:
        visible_count += 1

    if self.ssh is None:
      empty.hide()
      view.show()
    elif kind in self._inflight and view.count() == 0:
      empty.setText("Loading {}…".format(_KIND_TITLES[kind].lower()))
      empty.show()
      view.hide()
    elif view.count() == 0:
      empty.setText("No {} available yet.".format(_KIND_TITLES[kind].lower()))
      empty.show()
      view.hide()
    elif visible_count == 0:
      empty.setText("No {} match your filter.".format(_KIND_TITLES[kind].lower()))
      empty.show()
      view.hide()
    else:
      empty.hide()
      view.show()

  # ── selection helpers ──────────────────────────────────────
  def _selected_row(self, kind):
    table = self._tables[kind]
    sel = table.selectionModel().selectedRows()
    if not sel:
      return None
    item = table.item(sel[0].row(), 0)
    return item.data(Qt.UserRole) if item else None

  def _selected_ident(self, kind):
    row = self._selected_row(kind)
    return du.cell(row, _IDENT_KEY[kind]) if row else ""

  def _update_buttons(self, kind):
    row = self._selected_row(kind)
    have = row is not None
    for (k, key), btn in self._buttons.items():
      if k != kind or key == "pull":
        continue
      btn.setEnabled(have)
    if kind == "containers" and have:
      state = du.state_kind(du.cell(row, "State"))
      running, paused = state == "running", state == "paused"
      self._buttons[(kind, "start")].setEnabled(state == "stopped")
      self._buttons[(kind, "stop")].setEnabled(running or paused)
      self._buttons[(kind, "restart")].setEnabled(running or paused or state == "stopped")
      self._buttons[(kind, "pause")].setEnabled(running)
      self._buttons[(kind, "unpause")].setEnabled(paused)
      self._buttons[(kind, "exec")].setEnabled(running)

  def _on_double_click(self, kind):
    if self._selected_row(kind) is not None:
      self._inspect(kind)

  # ── actions ────────────────────────────────────────────────
  def _current_kind(self):
    return list(du.LIST_COMMANDS)[self.sub_tabs.currentIndex()]

  def _fail(self, title, err, code=None):
    friendly = du.classify_error(err, code if code is not None else 1)
    QMessageBox.warning(self.window(), title, friendly or (err or "").strip() or "Command failed.")

  def _run_action(self, label, cmd, refresh_kinds, timeout=None, show_output=False):
    if self.ssh is None:
      return
    self.status_msg.emit("{}…".format(label))
    w = self._worker(cmd, timeout=timeout)

    def finished(out, err, code):
      if code != 0:
        self.status_msg.emit("{} failed".format(label))
        self._fail(label, err or out, code)
      else:
        self.status_msg.emit("{} — done".format(label))
        if show_output:
          self._open_text(label, (out + ("\n" + err if err.strip() else "")).strip() or "(no output)")
      for k in refresh_kinds:
        self.refresh(k)

    w.result.connect(finished)
    w.error.connect(lambda msg: (self.status_msg.emit("{} failed".format(label)),
                                 QMessageBox.warning(self.window(), label, msg)))
    w.start()

  def _open_text(self, title, text):
    dlg = DockerTextDialog(self.window(), title, text)
    dlg.setAttribute(Qt.WA_DeleteOnClose, True)
    self._dialogs.append(dlg)
    dlg.destroyed.connect(lambda _=None, d=dlg: self._dialogs.remove(d) if d in self._dialogs else None)
    dlg.show()

  def _container_action(self, action):
    name = self._selected_ident("containers")
    if not name:
      return
    try:
      cmd = du.container_action_command(action, name)
    except ValueError as e:
      QMessageBox.warning(self.window(), "Docker", str(e))
      return
    self._run_action("{} {}".format(action.capitalize(), name), cmd,
                     ["containers"], timeout=60)

  def _show_logs(self):
    name = self._selected_ident("containers")
    if not name or self.ssh is None:
      return
    try:
      dlg = DockerLogDialog(self.window(), self.ssh, name, self._sudo_user)
    except ValueError as e:
      QMessageBox.warning(self.window(), "Docker", str(e))
      return
    dlg.setAttribute(Qt.WA_DeleteOnClose, True)
    self._dialogs.append(dlg)
    dlg.destroyed.connect(lambda _=None, d=dlg: self._dialogs.remove(d) if d in self._dialogs else None)
    dlg.show()

  def _run_in_container(self):
    name = self._selected_ident("containers")
    if not name:
      return
    text, ok = QInputDialog.getText(
      self.window(), "Run command",
      "Command to run inside '{}' (non-interactive, via sh -c):".format(name))
    if not ok or not text.strip():
      return
    try:
      cmd = du.exec_command(name, text)
    except ValueError as e:
      QMessageBox.warning(self.window(), "Docker", str(e))
      return
    self._run_action("Run in {}".format(name), cmd, [], timeout=120, show_output=True)

  def _inspect(self, kind):
    ident = self._selected_ident(kind)
    if not ident or self.ssh is None:
      return
    try:
      cmd = du.inspect_command(kind, ident)
    except ValueError as e:
      QMessageBox.warning(self.window(), "Docker", str(e))
      return
    w = self._worker(cmd)

    def finished(out, err, code):
      if code != 0:
        self._fail("Inspect", err or out, code)
        return
      try:
        text = json.dumps(json.loads(out), indent=2)
      except ValueError:
        text = out
      self._open_text("Inspect — {}".format(ident), text)

    w.result.connect(finished)
    w.error.connect(lambda msg: QMessageBox.warning(self.window(), "Inspect", msg))
    w.start()

  def _remove(self, kind):
    row = self._selected_row(kind)
    if row is None:
      return
    ident = du.cell(row, _IDENT_KEY[kind])
    label = du.image_display_ref(row) if kind == "images" else ident
    singular = _KIND_TITLES[kind][:-1].lower()
    force = False
    msg = "Remove {} '{}'? This cannot be undone.".format(singular, label)
    if kind == "containers" and du.state_kind(du.cell(row, "State")) in ("running", "paused"):
      force = True
      msg = ("Container '{}' is {}. Force-remove it? It will be killed and deleted "
             "(its anonymous volumes are kept).").format(label, du.cell(row, "State"))
    elif kind == "images":
      msg += "\n\nImages used by a container will be refused by Docker."
    if QMessageBox.question(self.window(), "Remove " + singular, msg,
                            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
      return
    try:
      cmd = {
        "containers": lambda: du.remove_container_command(ident, force=force),
        "images": lambda: du.remove_image_command(ident),
        "volumes": lambda: du.remove_volume_command(ident),
        "networks": lambda: du.remove_network_command(ident),
      }[kind]()
    except ValueError as e:
      QMessageBox.warning(self.window(), "Docker", str(e))
      return
    self._run_action("Remove {} {}".format(singular, label), cmd, [kind], timeout=60)

  def _pull_image(self):
    ref, ok = QInputDialog.getText(
      self.window(), "Pull image", "Image to pull (e.g. nginx:latest):")
    if not ok or not ref.strip():
      return
    try:
      cmd = du.pull_command(ref)
    except ValueError as e:
      QMessageBox.warning(self.window(), "Docker", str(e))
      return
    self._run_action("Pull {}".format(ref.strip()), cmd, ["images"],
                     timeout=900, show_output=True)

  def closeEvent(self, event):
    self._timer.stop()
    super().closeEvent(event)
