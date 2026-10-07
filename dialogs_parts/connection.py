from .common import *
from .efs import EFSConnectDialog

class MarqueeLabel(QWidget):
  """A single-line label that behaves like a normal elided label, but
  scrolls its text like a ticker on hover if the full text doesn't fit
  — so a long instance name or host is fully readable without stretching
  the card around it. Has a tiny fixed size hint on purpose, so it never
  dictates its container's width; it just fills whatever space it's given."""

  def __init__(self, text: str, color: str, px: int, bold: bool = False, parent=None):
    super().__init__(parent)
    self._text = text
    self._color = QColor(color)
    self._font = QFont()
    self._font.setPixelSize(px)
    self._font.setBold(bold)
    self._gap = 28
    self._offset = 0
    self._hovering = False
    self.setFont(self._font)
    self.setFixedHeight(px + 6)
    self.setMouseTracking(True)
    self.setToolTip(text)
    self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    self._timer = QTimer(self)
    self._timer.setInterval(30)
    self._timer.timeout.connect(self._advance)

  def sizeHint(self):
    return QSize(1, self.height())

  def minimumSizeHint(self):
    return QSize(1, self.height())

  def _text_width(self) -> int:
    return QFontMetrics(self._font).horizontalAdvance(self._text)

  def _overflowing(self) -> bool:
    return self._text_width() > self.width()

  def enterEvent(self, event):
    self._hovering = True
    if self._overflowing():
      self._offset = 0
      self._timer.start()
    super().enterEvent(event)

  def leaveEvent(self, event):
    self._hovering = False
    self._timer.stop()
    self._offset = 0
    self.update()
    super().leaveEvent(event)

  def _advance(self):
    self._offset += 2
    if self._offset > self._text_width() + self._gap:
      self._offset = 0
    self.update()

  def paintEvent(self, event):
    painter = QPainter(self)
    painter.setFont(self._font)
    painter.setPen(self._color)
    fm = painter.fontMetrics()
    y = (self.height() + fm.ascent() - fm.descent()) // 2
    if self._hovering and self._overflowing():
      tw = self._text_width()
      x = -self._offset
      painter.drawText(x, y, self._text)
      painter.drawText(x + tw + self._gap, y, self._text)
    else:
      painter.drawText(0, y, fm.elidedText(self._text, Qt.ElideRight, self.width()))
    painter.end()


class _RecentCard(QFrame):
  """Clickable card frame for one entry in the Recent Instances grid.
  Clicking anywhere on the card (outside the Connect button) fills the
  form below; the Connect button fills and connects immediately."""
  clicked = pyqtSignal()
  doubleClicked = pyqtSignal()

  def mousePressEvent(self, event):
    if event.button() == Qt.LeftButton:
      self.clicked.emit()
    super().mousePressEvent(event)

  def mouseDoubleClickEvent(self, event):
    if event.button() == Qt.LeftButton:
      self.doubleClicked.emit()
    super().mouseDoubleClickEvent(event)


class ConnectDialog(QDialog):
  def __init__(self, parent=None):
    super().__init__(parent)
    self.setWindowTitle("Connect to Server")
    self.setFixedWidth(500)
    apply_qss_to(self)

    layout = QVBoxLayout(self)
    layout.setSpacing(10)
    layout.setContentsMargins(24, 24, 24, 24)

    title = QLabel("Server Connection")
    title.setFont(QFont("Segoe UI", 16, QFont.Bold))
    title.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; margin-bottom: 4px;")
    layout.addWidget(title)

    # ── Recent instances ──────────────────────────────────
    recent = load_recent_instances()
    if recent:
      recent_lbl = QLabel("RECENT INSTANCES")
      recent_lbl.setStyleSheet(
        f"color: {T['TEXT_MUTED']}; font-size: 10px; font-weight: 700; "
        f"letter-spacing: 1px; padding: 4px 0 2px 0;"
      )
      layout.addWidget(recent_lbl)

      scroll = QScrollArea()
      scroll.setObjectName("recent_scroll")
      scroll.setWidgetResizable(True)
      scroll.setMaximumHeight(230)
      scroll.setFrameShape(QFrame.NoFrame)
      scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

      grid_host = QWidget()
      grid_host.setStyleSheet("background: transparent;")
      grid = QGridLayout(grid_host)
      grid.setContentsMargins(0, 0, 4, 0)
      grid.setHorizontalSpacing(8)
      grid.setVerticalSpacing(8)
      for col in (0, 1):
        grid.setColumnStretch(col, 1)

      for idx, inst in enumerate(recent):
        card = self._build_recent_card(inst)
        grid.addWidget(card, idx // 2, idx % 2)

      scroll.setWidget(grid_host)
      layout.addWidget(scroll)
      self.recent_list = grid_host  # kept for a truthy "has recent" check elsewhere

      div = QFrame()
      div.setFrameShape(QFrame.HLine)
      div.setStyleSheet(f"color: {T['BORDER']}; margin: 4px 0;")
      layout.addWidget(div)
    else:
      self.recent_list = None

    fields_lbl = QLabel("CONNECTION DETAILS")
    fields_lbl.setStyleSheet(
      f"color: {T['TEXT_MUTED']}; font-size: 10px; font-weight: 700; "
      f"letter-spacing: 1px; padding: 2px 0;"
    )
    layout.addWidget(fields_lbl)

    self.protocol_input = QComboBox()
    self.protocol_input.addItems(["SSH / SFTP", "FTP", "FTPS"])
    self.protocol_input.currentIndexChanged.connect(self._protocol_changed)
    layout.addWidget(QLabel("Protocol"))
    layout.addWidget(self.protocol_input)

    self.host_input = self._field("ec2-xx-xx-xx-xx.compute.amazonaws.com")
    self.port_input = self._field("22")
    import getpass
    self.user_input = self._field(getpass.getuser())
    self.pem_input  = self._field("/home/user/.ssh/key.pem")
    self.password  = self._field("password", password=True)
    self.alias_input = self._field("e.g. prod-web, staging-db (optional)")
    self._efs_values = None

    pem_row = QHBoxLayout()
    pem_row.setSpacing(6)
    pem_row.addWidget(self.pem_input)
    browse = QPushButton("Browse")
    browse.setFixedWidth(70)
    browse.clicked.connect(self._browse_pem)
    pem_row.addWidget(browse)

    for label, field in [
      ("Host / IP", self.host_input),
      ("Port",    self.port_input),
      ("Username",  self.user_input),
      ("Password",  self.password),
    ]:
      layout.addWidget(QLabel(label))
      layout.addWidget(field)

    layout.addWidget(QLabel("PEM Key (leave blank for password / agent auth)"))
    layout.addLayout(pem_row)

    layout.addWidget(QLabel("Alias (optional — shown in recent list and status bar)"))
    layout.addWidget(self.alias_input)

    layout.addSpacing(6)

    localhost_btn = icon_button(" Connect to Localhost")
    localhost_btn.setObjectName("success")
    localhost_btn.clicked.connect(self._fill_localhost)
    layout.addWidget(localhost_btn)

    efs_btn = icon_button(" Connect to AWS EFS")
    efs_btn.setObjectName("primary")
    efs_btn.clicked.connect(self._open_efs_dialog)
    layout.addWidget(efs_btn)
    layout.addSpacing(4)

    btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    btns.button(QDialogButtonBox.Ok).setText("Connect")
    btns.button(QDialogButtonBox.Ok).setObjectName("primary")
    btns.accepted.connect(self.accept)
    btns.rejected.connect(self.reject)
    layout.addWidget(btns)

  def _protocol_changed(self, index):
    protocol = ("ssh", "ftp", "ftps")[index]
    self.pem_input.setEnabled(protocol == "ssh")
    self.password.setEnabled(True)
    if protocol == "ssh":
      self.port_input.setText("22")
    elif protocol == "ftp":
      self.port_input.setText("21")
    else:
      self.port_input.setText("21")

  def _field(self, hint: str = "", password: bool = False) -> QLineEdit:
    w = QLineEdit()
    w.setPlaceholderText(hint)
    if password:
      w.setEchoMode(QLineEdit.Password)
    return w

  def _browse_pem(self):
    path, _ = QFileDialog.getOpenFileName(
      self, "Select PEM Key", "",
      "Private Key Files (*.pem *.privkey);;All Files (*)"
    )
    if path:
      self.pem_input.setText(path)

  def _build_recent_card(self, inst: dict) -> QWidget:
    """Build one grid card for the Recent Instances box: an icon badge,
    a bold title (alias, falling back to host), a muted host/IP subtitle,
    small pills for protocol / auth method, and a Connect button that
    fills the form and connects immediately without an extra click."""
    card = _RecentCard()
    card.setObjectName("recent_card")
    card.setCursor(Qt.PointingHandCursor)
    card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    card.setStyleSheet(
      f"""
      QFrame#recent_card {{
        background: {T['BG_ITEM']}; border: 1px solid {T['BORDER']};
        border-radius: 10px;
      }}
      QFrame#recent_card:hover {{
        border: 1px solid {T['ACCENT']};
      }}
      """
    )
    v = QVBoxLayout(card)
    v.setContentsMargins(12, 10, 12, 10)
    v.setSpacing(8)

    alias    = inst.get("alias", "").strip()
    host     = inst.get("host", "")
    user     = inst.get("user", "")
    port     = inst.get("port", "22")
    protocol = (inst.get("protocol") or "ssh").upper()

    # ── Header row: icon badge, title/subtitle, trailing pills ──
    header = QHBoxLayout()
    header.setSpacing(10)

    badge = QLabel()
    badge.setFixedSize(32, 32)
    badge.setAlignment(Qt.AlignCenter)
    badge.setStyleSheet(
      f"background: {_rgba(T['ACCENT'], 0.18)}; border-radius: 8px;"
    )
    badge.setPixmap(icon_pixmap("server", color=T["ACCENT"], size=16))
    header.addWidget(badge)

    text_col = QVBoxLayout()
    text_col.setSpacing(0)

    title = MarqueeLabel(alias if alias else host, T["TEXT_PRIMARY"], px=14, bold=True)
    title.setToolTip(f"{user}@{host}:{port}")
    text_col.addWidget(title)

    subtitle = MarqueeLabel(host, T["TEXT_MUTED"], px=11, bold=False)
    subtitle.setToolTip(f"{user}@{host}:{port}")
    text_col.addWidget(subtitle)

    header.addLayout(text_col, 1)

    if protocol != "SSH":
      proto_lbl = QLabel(protocol)
      proto_lbl.setStyleSheet(
        f"color: {T['INFO']}; background: {_rgba(T['INFO'], 0.16)}; "
        f"border-radius: 8px; padding: 1px 6px; font-size: 9px; font-weight: 700;"
      )
      header.addWidget(proto_lbl, 0, Qt.AlignTop)

    if inst.get("pem", "").strip():
      key_badge = QLabel()
      key_badge.setFixedSize(18, 18)
      key_badge.setAlignment(Qt.AlignCenter)
      key_badge.setToolTip("Connects using a PEM key")
      key_badge.setStyleSheet(
        f"background: {_rgba(T['TEXT_MUTED'], 0.14)}; border-radius: 9px;"
      )
      key_badge.setPixmap(icon_pixmap("key", color=T["TEXT_DIM"], size=10))
      header.addWidget(key_badge, 0, Qt.AlignTop)

    v.addLayout(header)

    # ── Quick-connect button ──
    connect_btn = QPushButton("Connect")
    connect_btn.setCursor(Qt.PointingHandCursor)
    connect_btn.setFixedHeight(26)
    connect_btn.setStyleSheet(
      f"""
      QPushButton {{
        background: {_rgba(T['ACCENT'], 0.16)}; color: {T['ACCENT2']};
        border: none; border-radius: 6px; font-size: 12px; font-weight: 600;
      }}
      QPushButton:hover {{ background: {_rgba(T['ACCENT'], 0.28)}; }}
      """
    )
    connect_btn.clicked.connect(lambda: self._quick_connect(inst))
    v.addWidget(connect_btn)

    # Clicking the card body (not the button) just fills the form for editing.
    card.clicked.connect(lambda: self._fill_from_recent_dict(inst))
    card.doubleClicked.connect(lambda: self._quick_connect(inst))

    return card

  def _fill_from_recent_dict(self, inst: dict):
    protocol = inst.get("protocol", "ssh").lower()
    self.protocol_input.setCurrentIndex({"ssh": 0, "ftp": 1, "ftps": 2}.get(protocol, 0))
    self.host_input.setText(inst.get("host", ""))
    self.port_input.setText(inst.get("port", "22"))
    self.user_input.setText(inst.get("user", ""))
    self.pem_input.setText(inst.get("pem", ""))
    self.alias_input.setText(inst.get("alias", ""))

  def _quick_connect(self, inst: dict):
    """Fill the form from a recent-instance card and connect immediately
    (the Connect button / double-click path on a Recent Instances card)."""
    self._fill_from_recent_dict(inst)
    self.accept()

  def _open_efs_dialog(self):
    dlg = EFSConnectDialog(self)
    if dlg.exec_() == QDialog.Accepted:
      self._efs_values = dlg.values()
      self.accept()

  def efs_values(self):
    return self._efs_values

  def _fill_localhost(self):
    import getpass
    self.protocol_input.setCurrentIndex(0)
    self.host_input.setText("127.0.0.1")
    self.port_input.setText("22")
    self.user_input.setText(getpass.getuser())
    self.pem_input.clear()
    self.alias_input.clear()

  def values(self) -> tuple:
    """Returns (protocol, host, port, user, pem, password, alias)."""
    return (
      ("ssh", "ftp", "ftps")[self.protocol_input.currentIndex()],
      self.host_input.text().strip(),
      int(self.port_input.text().strip() or "22"),
      self.user_input.text().strip(),
      self.pem_input.text().strip() if self.protocol_input.currentIndex() == 0 else "",
      self.password.text().strip(),
      self.alias_input.text().strip(),
    )


class ConnectingDialog(QDialog):
  """Small status dialog shown while an SSH connection attempt is in
  progress. Has no Cancel/Close button — it is dismissed programmatically
  by the caller once the attempt succeeds or fails."""

  def __init__(self, parent, host: str):
    super().__init__(parent)
    self.setWindowTitle("Connecting")
    self.setFixedSize(340, 150)
    self.setModal(True)
    # No close ("X") button — this dialog is closed programmatically.
    self.setWindowFlags(
      Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint
    )
    apply_qss_to(self)
    self.setStyleSheet(self.styleSheet() + f"QDialog {{ background: {T['BG_PANEL']}; }}")

    layout = QVBoxLayout(self)
    layout.setContentsMargins(28, 28, 28, 24)
    layout.setSpacing(14)

    icon_lbl = QLabel("")
    icon_lbl.setAlignment(Qt.AlignCenter)
    icon_lbl.setFont(QFont("Segoe UI Emoji", 28))
    icon_lbl.setStyleSheet("background: transparent;")
    layout.addWidget(icon_lbl)

    self.text_lbl = QLabel(f"Connecting to {host}…")
    self.text_lbl.setAlignment(Qt.AlignCenter)
    self.text_lbl.setWordWrap(True)
    self.text_lbl.setStyleSheet(
      f"background: transparent; color: {T['TEXT_PRIMARY']}; "
      f"font-size: 13px; font-weight: 600;"
    )
    layout.addWidget(self.text_lbl)

    self.bar = QProgressBar()
    self.bar.setRange(0, 0) # indeterminate / "busy" style
    self.bar.setTextVisible(False)
    self.bar.setFixedHeight(6)
    layout.addWidget(self.bar)

    sub_lbl = QLabel("This may take a few seconds…")
    sub_lbl.setAlignment(Qt.AlignCenter)
    sub_lbl.setStyleSheet(f"background: transparent; color: {T['TEXT_DIM']}; font-size: 13px;")
    layout.addWidget(sub_lbl)

  def set_status(self, text: str):
    self.text_lbl.setText(text)

  def closeEvent(self, event):
    # Prevent the user from dismissing it manually (e.g. Alt+F4);
    # the caller controls its lifecycle.
    event.ignore()

