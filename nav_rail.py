"""nav_rail.py — Left navigation rail (brand + primary navigation + quick access).

Pure presentation: it only emits ``navigate(key)`` and never touches app
state.  The main window maps keys to the existing tabs/actions, so no
behavior changes.  All colors come from the theme dict ``T``.
"""

from PyQt5.QtWidgets import QWidget, QVBoxLayout, QPushButton, QLabel, QFrame
from PyQt5.QtCore import Qt, pyqtSignal

from themes import T, rgba
from ui_icons import set_icon

RAIL_WIDTH = 190

# key, label, icon asset
NAV_ITEMS = [
  ("dashboard",  "Dashboard",  "dashboard"),
  ("kubernetes", "Kubernetes", "kubernetes"),
  ("ec2",        "EC2",        "server"),
  ("files",      "Files",      "folder"),
  ("terminal",   "Terminal",   "terminal"),
  ("tunnels",    "Tunnels",    "tunnel"),
]


class NavRail(QFrame):
  navigate = pyqtSignal(str)

  def __init__(self, parent=None):
    super().__init__(parent)
    self.setObjectName("kd_rail")
    self.setAttribute(Qt.WA_StyledBackground, True)
    self.setFixedWidth(RAIL_WIDTH)
    self._active = None
    self._buttons = {}

    lay = QVBoxLayout(self)
    lay.setContentsMargins(10, 14, 10, 8)
    lay.setSpacing(2)

    self.brand = QLabel("KubeDock")
    self.tagline = QLabel("INFRASTRUCTURE")
    lay.addWidget(self.brand)
    lay.addWidget(self.tagline)
    lay.addSpacing(12)

    for key, label, icon in NAV_ITEMS:
      b = QPushButton(label)
      b.setCursor(Qt.PointingHandCursor)
      b.setFocusPolicy(Qt.NoFocus)
      b.setProperty("icon_name", icon)
      b.clicked.connect(lambda _=False, k=key: self.navigate.emit(k))
      lay.addWidget(b)
      self._buttons[key] = b

    lay.addSpacing(10)
    # Quick-access Sidebar (Files context) is inserted here by the window.
    self._slot = QVBoxLayout()
    self._slot.setContentsMargins(0, 0, 0, 0)
    lay.addLayout(self._slot)
    lay.addStretch(1)
    self.refresh_theme()

  def add_quick_access(self, widget: QWidget):
    self._slot.addWidget(widget)

  def set_active(self, key):
    if key != self._active:
      self._active = key
      self._restyle()

  def _btn_qss(self, selected: bool) -> str:
    if selected:
      bg, fg, weight = rgba(T["ACCENT"], 0.18), T["TEXT_PRIMARY"], 700
    else:
      bg, fg, weight = "transparent", T["TEXT_DIM"], 500
    hover = rgba(T["ACCENT"], 0.24) if selected else T["BG_HOVER"]
    return (
      f"QPushButton {{ background: {bg}; color: {fg}; border: none; "
      f"border-radius: 7px; text-align: left; padding: 7px 10px; "
      f"font-size: 12px; font-weight: {weight}; }} "
      f"QPushButton:hover {{ background: {hover}; color: {T['TEXT_PRIMARY']}; }}"
    )

  def _restyle(self):
    for key, b in self._buttons.items():
      sel = key == self._active
      b.setStyleSheet(self._btn_qss(sel))
      set_icon(b, b.property("icon_name"),
               T["ACCENT2"] if sel else T["TEXT_MUTED"], 16)

  def refresh_theme(self):
    self.setStyleSheet(
      f"QFrame#kd_rail {{ background: {T['BG_SIDEBAR']}; "
      f"border-right: 1px solid {T['BORDER']}; }}"
    )
    self.brand.setStyleSheet(
      f"color: {T['TEXT_PRIMARY']}; font-size: 20px; font-weight: 800; "
      f"padding-left: 4px;")
    self.tagline.setStyleSheet(
      f"color: {T['TEXT_MUTED']}; font-size: 9px; font-weight: 700; "
      f"letter-spacing: 1.4px; padding-left: 4px;")
    self._restyle()
