"""Docker management tab.

Talks to the Docker CLI on the connected SSH host (the same host the
Kubernetes tab uses), so no extra connection or credentials are involved.
Command construction and output parsing live in docker_utils.py.
"""

import json

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
  QAbstractItemView, QCheckBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
  QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QTabWidget,
  QVBoxLayout, QWidget,
)

import docker_utils as du
from docker_dialogs import DockerLogDialog, DockerTextDialog
from themes import T
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


class DockerTab(QWidget):
  status_msg = pyqtSignal(str)

  def __init__(self, parent=None):
    super().__init__(parent)
    self.ssh = None
    self._sudo_user = None
    self._workers = []
    self._inflight = set()
    self._dirty = False
    self._tables = {}
    self._filters = {}
    self._buttons = {}      # (kind, key) -> QPushButton
    self._dialogs = []      # keep non-modal dialogs alive
    self._timer = QTimer(self)
    self._timer.timeout.connect(self._auto_refresh)
    self._build_ui()
    self._set_banner("Connect to a VM to manage its Docker containers.", error=False)

  # ── public API (mirrors KubernetesTab) ─────────────────────
  def set_ssh(self, ssh):
    self.ssh = ssh
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
    table = QTableWidget(0, len(cols))
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
    self._tables[kind] = table
    lay.addWidget(table, 1)
    return page

  def _restyle(self):
    self.title.setStyleSheet(
      "color: {}; font-size: 16px; font-weight: 700;".format(T["TEXT_PRIMARY"]))
    set_icon(self.refresh_btn, "refresh", T["TEXT_DIM"], 14)
    self.auto_chk.setStyleSheet("color: {};".format(T["TEXT_DIM"]))
    self._style_banner(self._banner_error)

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
    w = self._worker(du.list_command(kind))
    w.result.connect(lambda out, err, code, k=kind: self._on_listed(k, out, err, code))
    w.error.connect(lambda msg, k=kind: self._on_list_error(k, msg))
    w.start()

  def _on_listed(self, kind, out, err, code):
    self._inflight.discard(kind)
    if self.ssh is None:
      return
    if code != 0:
      friendly = du.classify_error(err, code)
      first = (err or "").strip().splitlines()[0] if (err or "").strip() else ""
      self._set_banner(friendly or first or "Docker command failed (exit {}).".format(code))
      return
    self._set_banner("")
    self._populate(kind, du.parse_json_lines(out))

  def _on_list_error(self, kind, msg):
    self._inflight.discard(kind)
    self._set_banner("Could not run docker: {}".format(msg))

  # ── table population ───────────────────────────────────────
  def _clear_tables(self):
    for table in self._tables.values():
      table.setRowCount(0)
    for kind in self._tables:
      self._update_buttons(kind)

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
    if reselect >= 0:
      table.selectRow(reselect)
    self._apply_filter(kind)
    self._update_buttons(kind)

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
      if not needle:
        table.setRowHidden(r, False)
        continue
      text = " ".join(
        (table.item(r, c).text() if table.item(r, c) else "")
        for c in range(table.columnCount())
      ).lower()
      table.setRowHidden(r, needle not in text)

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
