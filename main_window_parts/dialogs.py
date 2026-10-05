from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QLineEdit, QDialogButtonBox, QFrame, QPushButton,
    QAbstractItemView, QWidget, QGraphicsDropShadowEffect,
)
from PyQt5.QtCore import Qt, QEvent
from PyQt5.QtGui import QColor
from ui_icons import icon_button, icon_pixmap
from themes import T


def _themed_qss(qss: str) -> str:
  """Fill __KEY__ tokens in a literal stylesheet from the active theme T."""
  from themes import on_color
  out = qss.replace("__ONACC__", on_color(T["ACCENT"]))
  for key, val in T.items():
    out = out.replace("__%s__" % key, val)
  return out


class _TerminalPopoutWindow(QDialog):
  """Floating window that hosts the terminal when popped out. Behaves like
  a normal top-level window (resizable, minimizable, has its own close
  button) but reports back to the main window when closed — whether that
  happens via the 'X' button or programmatically — so the terminal widget
  can be re-docked into the main layout instead of being destroyed."""

  def __init__(self, parent, on_close):
    super().__init__(parent, Qt.Window)
    self._on_close = on_close
    self.setWindowTitle("Terminal")
    self.resize(760, 420)

  def closeEvent(self, event):
    self._on_close()
    event.accept()


class _UserPickerDialog(QDialog):
  """Lets the user pick from system accounts discovered via SSH, instead
  of typing a username blind. Falls back to manual entry for accounts
  that don't show up in /etc/passwd (LDAP/NIS, etc.)."""

  def __init__(self, parent, users, current=None):
    super().__init__(parent)
    self.setWindowTitle("Switch User")
    self.resize(320, 420)
    self.selected_user = None

    layout = QVBoxLayout(self)
    layout.addWidget(QLabel("Select a user to switch to:"))

    self.list_widget = QListWidget()
    self.list_widget.itemDoubleClicked.connect(lambda _: self.accept())
    for name, uid in users:
      label = "{} (root)".format(name) if uid == "0" else name
      item = QListWidgetItem(label)
      item.setData(Qt.UserRole, name)
      self.list_widget.addItem(item)
      if name == current:
        self.list_widget.setCurrentItem(item)
    layout.addWidget(self.list_widget)

    manual_row = QHBoxLayout()
    manual_row.addWidget(QLabel("Or enter manually:"))
    self.manual_edit = QLineEdit()
    self.manual_edit.setPlaceholderText("username")
    manual_row.addWidget(self.manual_edit)
    layout.addLayout(manual_row)

    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(self.accept)
    buttons.rejected.connect(self.reject)
    layout.addWidget(buttons)

  def accept(self):
    # Manual entry wins if filled in, so a typed name always overrides
    # an incidentally-still-selected list item.
    manual = self.manual_edit.text().strip()
    if manual:
      self.selected_user = manual
    else:
      item = self.list_widget.currentItem()
      if item:
        self.selected_user = item.data(Qt.UserRole)
    if not self.selected_user:
      return # nothing chosen — keep the dialog open
    super().accept()


class UserSwitchDialog(QDialog):
  """Elegant, frameless user picker for switching sudo context."""

  # A small rotating palette so avatars aren't all the same color.
  _AVATAR_COLORS = [
    ("#3730a3", "#c7d2fe"), # indigo
    ("#155e75", "#a5f3fc"), # cyan
    ("#7c2d12", "#fed7aa"), # amber
    ("#166534", "#bbf7d0"), # green
    ("#831843", "#fbcfe8"), # pink
    ("#4c1d95", "#ddd6fe"), # violet
    ("#78350f", "#fde68a"), # gold
    ("#0c4a6e", "#bae6fd"), # sky
  ]
  _ROOT_COLOR = ("#7f1d1d", "#fecaca") # red — reserved for root

  def __init__(self, parent, users, current_user=None):
    super().__init__(parent)

    self.users = users
    self.current_user = current_user
    self._selected_user = None
    self._drag_pos = None
    self._hovered_item = None
    self._selected_item_widget = None

    self.setWindowTitle("Switch User")
    self.setModal(True)
    self.setFixedSize(440, 560)
    # Frameless + translucent lets us draw our own rounded, shadowed
    # card instead of relying on the OS window chrome, which is what
    # made the previous version feel flat.
    self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
    self.setAttribute(Qt.WA_TranslucentBackground)

    outer = QVBoxLayout(self)
    outer.setContentsMargins(20, 20, 20, 20) # breathing room for the shadow
    outer.setSpacing(0)

    self.card = QFrame()
    self.card.setObjectName("card")

    shadow = QGraphicsDropShadowEffect(self.card)
    shadow.setBlurRadius(48)
    shadow.setOffset(0, 16)
    shadow.setColor(QColor(0, 0, 0, 170))
    self.card.setGraphicsEffect(shadow)

    outer.addWidget(self.card)

    self.setStyleSheet(_themed_qss("""
      #card {
        background: __BG_PANEL__;
        border: 1px solid __BORDER__;
        border-radius: 18px;
      }

      QLabel {
        background: transparent;
      }

      QLineEdit {
        background: __BG_ITEM__;
        color: __TEXT_PRIMARY__;
        border: 1px solid __BORDER__;
        border-radius: 10px;
        padding: 10px 13px 10px 34px;
        font-size: 13px;
        selection-background-color: __ACCENT__;
      }

      QLineEdit:focus {
        border: 1px solid __ACCENT__;
      }

      QListWidget {
        background: __BG_DARK__;
        border: 1px solid __BORDER__;
        border-radius: 12px;
        padding: 6px;
        outline: none;
      }

      /* The list used to paint its own hover/selected rectangle
        here, inset by a separate margin from the row widget's own
        padding — the two insets didn't match, so the highlight
        looked like a misaligned box floating around the row's
        actual content. Rows now draw their own highlight (see
        QFrame#userRow below) using the exact same padding as their
        content, so the list's own item box stays fully transparent
        and just acts as a plain, unstyled container. */
      QListWidget::item {
        background: transparent;
        border: none;
        padding: 0;
        margin: 0;
      }

      QListWidget::item:hover,
      QListWidget::item:selected,
      QListWidget::item:focus {
        background: transparent;
        border: none;
        outline: none;
      }

      QFrame#userRow {
        background: transparent;
        border-radius: 10px;
      }

      QFrame#userRow[rowHovered="true"] {
        background: __BG_HOVER__;
      }

      QFrame#userRow[rowSelected="true"] {
        background: __BG_ITEM_SEL__;
      }

      QPushButton {
        background: __BG_ITEM__;
        color: __TEXT_DIM__;
        border: 1px solid __BORDER__;
        border-radius: 9px;
        padding: 9px 18px;
        font-size: 13px;
        font-weight: 600;
      }

      QPushButton:hover {
        background: __BG_HOVER__;
        color: __TEXT_PRIMARY__;
      }

      QPushButton#switchButton {
        background: __ACCENT__;
        color: __ONACC__;
        border: none;
      }

      QPushButton#switchButton:hover {
        background: __ACCENT2__;
      }

      QPushButton#switchButton:disabled {
        background: __BG_ITEM__;
        color: __TEXT_MUTED__;
      }

      QPushButton#closeBtn {
        background: transparent;
        border: none;
        color: __TEXT_MUTED__;
        font-size: 13px;
        font-weight: 700;
        border-radius: 13px;
        padding: 0;
      }

      QPushButton#closeBtn:hover {
        background: __BG_HOVER__;
        color: __TEXT_PRIMARY__;
      }
    """))

    main = QVBoxLayout(self.card)
    main.setContentsMargins(22, 18, 22, 20)
    main.setSpacing(12)

    # ─────────────────────────────────────────────
    # Header (also the drag handle, since we're frameless)
    # ─────────────────────────────────────────────

    header_row = QHBoxLayout()
    header_row.setSpacing(8)

    title = QLabel("Switch User")
    title.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; font-size: 20px; font-weight: 700;")
    header_row.addWidget(title)
    header_row.addStretch()

    close_btn = icon_button("")
    close_btn.setObjectName("closeBtn")
    close_btn.setFixedSize(26, 26)
    close_btn.setCursor(Qt.PointingHandCursor)
    close_btn.setToolTip("Cancel")
    close_btn.clicked.connect(self.reject)
    header_row.addWidget(close_btn)

    main.addLayout(header_row)

    subtitle = QLabel("Choose the user context for terminal and file operations.")
    subtitle.setWordWrap(True)
    subtitle.setStyleSheet("color: #858993; font-size: 12px; padding-bottom: 4px;")
    main.addWidget(subtitle)

    # ─────────────────────────────────────────────
    # Current user badge
    # ─────────────────────────────────────────────

    if current_user:
      current_frame = QFrame()
      current_frame.setStyleSheet("""
        QFrame {
          background: #17191e;
          border: 1px solid #292c33;
          border-radius: 10px;
        }
      """)

      current_layout = QHBoxLayout(current_frame)
      current_layout.setContentsMargins(12, 8, 12, 8)
      current_layout.setSpacing(8)

      current_icon = QLabel("●")
      current_icon.setStyleSheet("color: #52d273; font-size: 11px;")

      current_text = QLabel(f"Currently acting as <b>{current_user}</b>")
      current_text.setStyleSheet("color: #aeb1ba; font-size: 12px;")

      current_layout.addWidget(current_icon)
      current_layout.addWidget(current_text)
      current_layout.addStretch()

      main.addWidget(current_frame)

    # ─────────────────────────────────────────────
    # Search
    # ─────────────────────────────────────────────

    search_wrap = QWidget()
    search_layout = QHBoxLayout(search_wrap)
    search_layout.setContentsMargins(0, 0, 0, 0)
    search_layout.setSpacing(0)

    self.search = QLineEdit()
    self.search.setPlaceholderText("Search users…")
    self.search.setClearButtonEnabled(True)
    self.search.textChanged.connect(self._filter_users)
    self.search.returnPressed.connect(self._accept_top_match)
    search_layout.addWidget(self.search)

    search_icon = QLabel(search_wrap)
    search_icon.setPixmap(icon_pixmap("search", size=15))
    search_icon.setStyleSheet("color: #6f7380; font-size: 12px; background: transparent;")
    search_icon.move(11, 11)

    main.addWidget(search_wrap)

    # ─────────────────────────────────────────────
    # User list
    # ─────────────────────────────────────────────

    self.list = QListWidget()
    self.list.setSelectionMode(QAbstractItemView.SingleSelection)
    # Rows no longer rely on the list's own item margin for spacing
    # (that margin was one half of the alignment mismatch) — use the
    # view's spacing instead, which sits *outside* each row and can't
    # conflict with the row's own internal padding.
    self.list.setSpacing(4)
    # Without mouse tracking, the view only gets mouseMove events while
    # a button is held down — so hover never fired on a plain hover.
    self.list.setMouseTracking(True)
    self.list.viewport().setMouseTracking(True)
    # Hover highlighting is now driven manually (see eventFilter /
    # _update_hover below) so each row can paint its own highlight
    # using its own padding, instead of the list drawing a separately
    # inset rectangle behind it.
    self.list.viewport().installEventFilter(self)
    self.list.itemDoubleClicked.connect(self._accept_selection)
    self.list.itemSelectionChanged.connect(self._selection_changed)

    main.addWidget(self.list, 1)

    self.empty_label = QLabel("No matching users")
    self.empty_label.setAlignment(Qt.AlignCenter)
    self.empty_label.setStyleSheet("color: #63666f; font-size: 12px; padding: 24px 0;")
    self.empty_label.hide()
    main.addWidget(self.empty_label)

    # ─────────────────────────────────────────────
    # Footer
    # ─────────────────────────────────────────────

    footer = QHBoxLayout()
    footer.setSpacing(8)

    self.count_label = QLabel()
    self.count_label.setStyleSheet("color: #686c76; font-size: 11px;")

    footer.addWidget(self.count_label)
    footer.addStretch()

    cancel_btn = QPushButton("Cancel")
    cancel_btn.setCursor(Qt.PointingHandCursor)
    cancel_btn.clicked.connect(self.reject)

    self.switch_btn = QPushButton("Switch →")
    self.switch_btn.setObjectName("switchButton")
    self.switch_btn.setCursor(Qt.PointingHandCursor)
    self.switch_btn.setEnabled(False)
    self.switch_btn.clicked.connect(self._accept_selection)

    footer.addWidget(cancel_btn)
    footer.addWidget(self.switch_btn)

    main.addLayout(footer)

    self._populate_users(users)
    self.search.setFocus()

  # ── Avatar helpers ───────────────────────────────────────────

  def _avatar_colors(self, user):
    if user == "root":
      return self._ROOT_COLOR
    idx = sum(ord(c) for c in user) % len(self._AVATAR_COLORS)
    return self._AVATAR_COLORS[idx]

  # ── Population / filtering ───────────────────────────────────

  def _populate_users(self, users):
    self.list.clear()
    # The list is about to delete every item/row widget — drop any
    # references to them so hover/selection tracking doesn't touch a
    # deleted widget on the next event.
    self._hovered_item = None
    self._selected_item_widget = None

    selectable = [u for u in users if u and u != self.current_user]

    for user in selectable:
      item = QListWidgetItem()
      item.setData(Qt.UserRole, user)

      # A QFrame (not a plain QWidget) so it can carry its own
      # rounded, stateful highlight — see QFrame#userRow in the
      # stylesheet. This is what the row's hover/selected color is
      # actually painted on, using the row's own padding, so the
      # highlight can't drift out of alignment with its content the
      # way a separately-inset list-drawn highlight could.
      widget = QFrame()
      widget.setObjectName("userRow")
      widget.setProperty("rowHovered", False)
      widget.setProperty("rowSelected", False)
      widget.setMinimumHeight(48)
      # A plain QWidget (and the QLabels inside it) accepts mouse
      # events by default, which means a click on the row was being
      # swallowed by the item widget instead of reaching the
      # QListWidget underneath — so clicking a user did nothing;
      # only a click on the thin unpainted edge of the row actually
      # selected it. Marking the row (and everything in it)
      # transparent to mouse events lets clicks fall straight
      # through to the list widget's own selection handling, so a
      # single click anywhere on a row now selects that user.
      widget.setAttribute(Qt.WA_TransparentForMouseEvents)

      row = QHBoxLayout(widget)
      row.setContentsMargins(10, 5, 10, 5)
      row.setSpacing(10)

      # Avatar
      bg, fg = self._avatar_colors(user)
      avatar = QLabel(user[:1].upper())
      avatar.setAlignment(Qt.AlignCenter)
      avatar.setFixedSize(32, 32)
      avatar.setStyleSheet(f"""
        QLabel {{
          background: {bg};
          color: {fg};
          border-radius: 16px;
          font-size: 12px;
          font-weight: 700;
        }}
      """)

      # Username (+ root badge)
      name_col = QVBoxLayout()
      name_col.setSpacing(0)

      name = QLabel(user)
      name.setStyleSheet("QLabel { color: #eeeeF2; font-size: 13px; font-weight: 600; }")
      name_col.addWidget(name)

      if user == "root":
        badge = QLabel("full system access")
        badge.setStyleSheet("QLabel { color: #f87171; font-size: 10px; font-weight: 600; }")
        name_col.addWidget(badge)

      row.addWidget(avatar)
      row.addLayout(name_col)
      row.addStretch()

      arrow = QLabel("›")
      arrow.setStyleSheet("color: #454852; font-size: 15px;")
      row.addWidget(arrow)

      # setAttribute(WA_TransparentForMouseEvents) on the row widget
      # alone doesn't cascade to its children — the avatar/name/arrow
      # QLabels still individually catch the mouse first (they're on
      # top in z-order), so hover and clicks never reached the list
      # underneath except on the few unlabeled pixels of the row.
      # Marking every child transparent too closes that gap.
      for child in widget.findChildren(QWidget):
        child.setAttribute(Qt.WA_TransparentForMouseEvents)

      # Without an explicit size hint the row widget's height is
      # ignored and rows overlap — this was the main cause of the
      # cramped, broken-looking list.
      item.setSizeHint(widget.sizeHint())

      self.list.addItem(item)
      self.list.setItemWidget(item, widget)

    # Previously auto-selected row 0 (usually "root") and enabled
    # Switch before the user had picked anyone — so the button was
    # live and clickable while nothing visibly showed as selected.
    # Leave nothing selected until the user actually clicks a row.
    self.list.setCurrentRow(-1)
    self.switch_btn.setEnabled(False)

    self._update_counts()

  def _filter_users(self, text):
    text = text.strip().lower()
    first_visible = None

    for i in range(self.list.count()):
      item = self.list.item(i)
      username = (item.data(Qt.UserRole) or "").lower()
      hidden = bool(text) and text not in username
      item.setHidden(hidden)
      if not hidden and first_visible is None:
        first_visible = item

    if first_visible is not None:
      self.list.setCurrentItem(first_visible)
    else:
      self.list.setCurrentItem(None)
      self._selected_user = None
      self.switch_btn.setEnabled(False)

    self._update_counts()

  def _update_counts(self):
    visible = sum(
      1 for i in range(self.list.count()) if not self.list.item(i).isHidden()
    )
    total = self.list.count()
    if self.search.text().strip():
      self.count_label.setText(f"{visible} of {total} users")
    else:
      self.count_label.setText(f"{total} users")
    self.empty_label.setVisible(total > 0 and visible == 0)
    self.list.setVisible(not (total > 0 and visible == 0))

  def _repolish(self, widget):
    # Changing a dynamic property doesn't repaint on its own — the
    # stylesheet has to be re-evaluated against the widget first.
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()

  def _selection_changed(self):
    item = self.list.currentItem()

    if self._selected_item_widget is not None:
      self._selected_item_widget.setProperty("rowSelected", False)
      self._repolish(self._selected_item_widget)
      self._selected_item_widget = None

    if item and not item.isHidden():
      self._selected_user = item.data(Qt.UserRole)
      self.switch_btn.setEnabled(bool(self._selected_user))
      row = self.list.itemWidget(item)
      if row is not None:
        row.setProperty("rowSelected", True)
        self._repolish(row)
        self._selected_item_widget = row
    else:
      self._selected_user = None
      self.switch_btn.setEnabled(False)

  def _update_hover(self, pos):
    item = self.list.itemAt(pos) if pos is not None else None
    if item is self._hovered_item:
      return

    if self._hovered_item is not None:
      old_row = self.list.itemWidget(self._hovered_item)
      if old_row is not None:
        old_row.setProperty("rowHovered", False)
        self._repolish(old_row)

    self._hovered_item = item

    if item is not None and not item.isHidden():
      new_row = self.list.itemWidget(item)
      if new_row is not None:
        new_row.setProperty("rowHovered", True)
        self._repolish(new_row)

  def eventFilter(self, obj, event):
    if obj is self.list.viewport():
      if event.type() == QEvent.MouseMove:
        self._update_hover(event.pos())
      elif event.type() == QEvent.Leave:
        self._update_hover(None)
    return super().eventFilter(obj, event)

  def _accept_selection(self):
    item = self.list.currentItem()

    if not item or item.isHidden():
      return

    username = item.data(Qt.UserRole)

    if username:
      self._selected_user = username
      self.accept()

  def _accept_top_match(self):
    # Enter in the search box switches to whatever is currently
    # highlighted (the first match), same as pressing "Switch".
    self._accept_selection()

  def selected_user(self):
    return self._selected_user

  # ── Frameless window dragging ────────────────────────────────

  def mousePressEvent(self, event):
    if event.button() == Qt.LeftButton:
      self._drag_pos = event.globalPos() - self.frameGeometry().topLeft()
      event.accept()

  def mouseMoveEvent(self, event):
    if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
      self.move(event.globalPos() - self._drag_pos)
      event.accept()

  def mouseReleaseEvent(self, event):
    self._drag_pos = None
