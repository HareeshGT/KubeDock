"""ui_kit.py — Shared KubeDock design-system primitives.

Everything here reads colors from the existing theme dict ``T`` at call time,
so any widget rebuilt (or ``refresh_theme()``-d) after a theme switch picks up
the active palette.  No second theme system: this module only derives
tints/borders/states from the same BG_*/ACCENT/SUCCESS/... keys.

Design tokens
-------------
  RADIUS      6   controls, badges, buttons
  RADIUS_CARD 8   cards / panels
  Type scale  page 22 · section 15 · card 13 · body 12 · secondary 11
"""

from PyQt5.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QLabel, QWidget
from PyQt5.QtCore import Qt

from themes import T, rgba  # rgba re-exported for callers

RADIUS = 6
RADIUS_CARD = 8

FS_PAGE, FS_SECTION, FS_CARD, FS_BODY, FS_SMALL = 22, 15, 13, 12, 11


# ─── Color helpers ────────────────────────────────────────────
def status_color(state) -> str:
  """Map a semantic state to a theme color.

  ok/active/running  -> SUCCESS (green)
  fail/stopped/error -> DANGER  (red)
  pending/connecting -> WARNING (yellow)
  anything else      -> TEXT_MUTED
  """
  s = str(state or "").lower()
  if s in ("ok", "active", "running", "healthy", "ready", "success", "bound", "true"):
    return T["SUCCESS"]
  if s in ("fail", "failed", "stopped", "error", "down", "inactive", "danger", "false"):
    return T["DANGER"]
  if s in ("pending", "connecting", "checking", "warning", "unknown_pending"):
    return T["WARNING"]
  return T["TEXT_MUTED"]


# ─── QSS fragments ────────────────────────────────────────────
def card_qss(obj_name: str, selected: bool = False, accent_edge: str = "") -> str:
  """Frame style shared by every resource card (Services, Tunnels, ...)."""
  border = T["ACCENT"] if selected else T["BORDER"]
  bg = T["BG_ITEM_SEL"] if selected else T["BG_ITEM"]
  left = f"border-left: 3px solid {accent_edge}; " if accent_edge else ""
  return (
    f"QFrame#{obj_name} {{ background: {bg}; border: 1px solid {border}; "
    f"{left}border-radius: {RADIUS_CARD}px; }} "
    f"QFrame#{obj_name}:hover {{ border-color: {T['ACCENT']}; "
    f"{left}}}"
  )


def pill_qss(color: str) -> str:
  return (
    f"background: {rgba(color, 0.14)}; color: {color}; "
    f"border: 1px solid {rgba(color, 0.35)}; border-radius: {RADIUS + 3}px; "
    f"padding: 1px 8px; font-size: {FS_SMALL}px; font-weight: 600;"
  )


def chip_qss() -> str:
  return (
    f"background: {T['BG_DARK']}; color: {T['TEXT_DIM']}; "
    f"border: 1px solid {T['BORDER']}; border-radius: {RADIUS}px; "
    f"padding: 1px 7px; font-size: {FS_SMALL}px; font-weight: 600;"
  )


# ─── Small widgets ────────────────────────────────────────────
class StatusBadge(QLabel):
  """Pill with a leading dot, e.g. '● Running'. Re-tints via ``set_state``."""

  def __init__(self, text: str = "", state="", parent=None):
    super().__init__(parent)
    self.set_state(state, text)

  def set_state(self, state, text: str = None):
    color = status_color(state)
    if text is not None:
      self.setText(f"\u25cf  {text}")
    self.setStyleSheet(pill_qss(color))
    self.setAlignment(Qt.AlignCenter)


class StatusDot(QLabel):
  """Bare colored dot for dense lists."""

  def __init__(self, state="", parent=None):
    super().__init__("\u25cf", parent)
    self.set_state(state)

  def set_state(self, state):
    self.setStyleSheet(f"color: {status_color(state)}; font-size: 14px;")


class Chip(QLabel):
  def __init__(self, text: str = "", parent=None):
    super().__init__(text, parent)
    self.setStyleSheet(chip_qss())


class PageHeader(QWidget):
  """Page title + optional subtitle with a right-aligned action area."""

  def __init__(self, title: str, subtitle: str = "", parent=None):
    super().__init__(parent)
    row = QHBoxLayout(self)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(10)
    col = QVBoxLayout()
    col.setSpacing(1)
    self.title_lbl = QLabel(title)
    self.sub_lbl = QLabel(subtitle)
    self.sub_lbl.setVisible(bool(subtitle))
    col.addWidget(self.title_lbl)
    col.addWidget(self.sub_lbl)
    row.addLayout(col)
    row.addStretch(1)
    self.actions = QHBoxLayout()
    self.actions.setSpacing(6)
    row.addLayout(self.actions)
    self.refresh_theme()

  def refresh_theme(self):
    self.title_lbl.setStyleSheet(
      f"color: {T['TEXT_PRIMARY']}; font-size: {FS_PAGE}px; font-weight: 700;")
    self.sub_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: {FS_BODY}px;")


class SectionHeader(QLabel):
  def __init__(self, text: str, parent=None):
    super().__init__(text, parent)
    self.refresh_theme()

  def refresh_theme(self):
    self.setStyleSheet(
      f"color: {T['TEXT_PRIMARY']}; font-size: {FS_SECTION}px; font-weight: 700;")


class ResourceCard(QFrame):
  """Base for Services / Tunnels style cards: themed frame + hover accent.

  Subclasses build their layout, then call ``self._apply_styles()``; override
  ``_style_children()`` for per-widget styling.  ``refresh_theme()`` re-runs it
  so cards follow theme switches without being rebuilt.
  """

  OBJ = "resource_card"

  def __init__(self, parent=None):
    super().__init__(parent)
    self.setObjectName(self.OBJ)
    self._selected = False

  def _style_children(self):
    pass

  def _apply_styles(self):
    self.setStyleSheet(card_qss(self.OBJ, self._selected))
    self._style_children()

  def refresh_theme(self):
    self._apply_styles()
