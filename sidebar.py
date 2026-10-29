"""sidebar.py — Quick-access Sidebar widget."""

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QPushButton, QLabel
from PyQt5.QtCore import Qt, pyqtSignal

from ui_icons import set_icon, split_icon_text
from themes import T, rgba


class Sidebar(QWidget):
  navigate = pyqtSignal(str)

  QUICK_ALWAYS = [
    (" Home", ""), (" Root", "/"), (" Tmp", "/tmp"),
  ]
  QUICK_PROBE = [
    (" Etc",     "/etc"),
    (" Var",     "/var"),
    (" Opt",     "/opt"),
    (" Srv",     "/srv"),
    (" Usr/local",  "/usr/local"),
    (" Applications", "/Applications"),
    (" Library",   "/Library"),
    (" Users",    "/Users"),
    (" Volumes",   "/Volumes"),
  ]

  def __init__(self, parent=None):
    super().__init__(parent)
    self.setAttribute(Qt.WA_StyledBackground, True)
    self.setFixedWidth(180)
    self._refresh_bg()
    self._layout = QVBoxLayout(self)
    self._layout.setContentsMargins(0, 8, 0, 8)
    self._layout.setSpacing(0)

    self._active = None   # path of the last-navigated item (selected state)
    self._buttons = []    # (button, path) for restyling on selection/theme
    lbl = QLabel("QUICK ACCESS")
    lbl.setObjectName("section_title")
    self._layout.addWidget(lbl)

    # Keep common locations visible and group the less-frequent paths.
    self._expanded = False
    self._sys_lbl = QPushButton("MORE LOCATIONS ▾")
    self._sys_lbl.setObjectName("quick_access_more")
    self._sys_lbl.setFlat(True)
    self._sys_lbl.setCursor(Qt.PointingHandCursor)
    self._sys_lbl.setStyleSheet(
      f"color: {T['TEXT_MUTED']}; text-align: left; border: none; "
      "padding: 8px 14px 4px; font-size: 9px; font-weight: 700; "
      "letter-spacing: 1px; background: transparent;"
    )
    self._sys_lbl.clicked.connect(self._toggle_probe_locations)
    self._sys_lbl.hide()
    self._layout.addWidget(self._sys_lbl)
    self._layout.addStretch()
    self._probe_buttons = []
    for text, path in self.QUICK_ALWAYS:
      self._add_btn(text, path, probe=False)

  # ── Internal helpers ──────────────────────────────────────
  def set_embedded(self, on: bool):
    """When hosted inside the NavRail the rail draws bg + border."""
    self._embedded = on
    self._refresh_bg()

  def _refresh_bg(self):
    if getattr(self, "_embedded", False):
      self.setStyleSheet("QWidget#kd_sidebar { background: transparent; border: none; }")
      return
    self.setObjectName("kd_sidebar")
    self.setStyleSheet(
      f"QWidget#kd_sidebar {{ background: {T['BG_SIDEBAR']}; "
      f"border-right: 1px solid {T['BORDER']}; }}"
    )

  def _btn_style(self, selected: bool = False) -> str:
    """Compact row; the selected item gets a restrained accent bar + tint."""
    if selected:
      bg, fg, bar = rgba(T['ACCENT'], 0.14), T['TEXT_PRIMARY'], T['ACCENT']
    else:
      bg, fg, bar = "transparent", T['TEXT_DIM'], "transparent"
    return f"""
      QPushButton {{
        background: {bg}; color: {fg};
        border: none; border-left: 2px solid {bar}; border-radius: 0;
        text-align: left; padding: 6px 14px; font-size: 12px;
        font-weight: {600 if selected else 500};
      }}
      QPushButton:hover  {{ background: {T['BG_HOVER'] if not selected else rgba(T['ACCENT'], 0.2)}; color: {T['TEXT_PRIMARY']}; }}
      QPushButton:pressed {{ background: {T['BG_ITEM_SEL']}; }}
    """

  def _set_active(self, path):
    self._active = path
    self._restyle_buttons()

  def _toggle_probe_locations(self):
    self._expanded = not self._expanded
    self._sys_lbl.setText("FEWER LOCATIONS ▴" if self._expanded else "MORE LOCATIONS ▾")
    for btn in self._probe_buttons:
      btn.setVisible(self._expanded)

  def _restyle_buttons(self):
    for btn, p in self._buttons:
      btn.setStyleSheet(self._btn_style(p == self._active))

  def _add_btn(self, text: str, path: str, probe: bool = True) -> QPushButton:
    btn = QPushButton()
    name, cleaned = split_icon_text(text)
    if name:
      set_icon(btn, name, size=16)
      btn.setText(cleaned)
    else:
      btn.setText(text)
    btn.setStyleSheet(self._btn_style(path == self._active))
    btn.setCursor(Qt.PointingHandCursor)
    btn.clicked.connect(lambda _, p=path: (self._set_active(p), self.navigate.emit(p)))
    idx = (self._layout.count() - 1) if probe else self._layout.indexOf(self._sys_lbl)
    self._layout.insertWidget(idx, btn)
    self._buttons.append((btn, path))
    return btn

  # ── Public API ────────────────────────────────────────────
  def populate_remote(self, sftp):
    """Probe common paths on the remote and add buttons for those that exist."""
    self._clear_probe_buttons()
    for text, path in self.QUICK_PROBE:
      try:
        sftp.stat(path)
        btn = self._add_btn(text, path)
        btn.setVisible(self._expanded)
        self._probe_buttons.append(btn)
      except Exception:
        pass
    self._sys_lbl.setVisible(bool(self._probe_buttons))
    self._sys_lbl.setText("FEWER LOCATIONS ▴" if self._expanded else "MORE LOCATIONS ▾")

  def clear_remote(self):
    self._clear_probe_buttons()

  def refresh_theme(self):
    self._refresh_bg()
    self._restyle_buttons()

  # ── Private ───────────────────────────────────────────────
  def _clear_probe_buttons(self):
    self._expanded = False
    self._sys_lbl.setText("MORE LOCATIONS ▾")
    self._sys_lbl.hide()
    for btn in self._probe_buttons:
      self._buttons = [(b, p) for b, p in self._buttons if b is not btn]
      self._layout.removeWidget(btn)
      btn.deleteLater()
    self._probe_buttons = []
    self._sys_lbl.hide()