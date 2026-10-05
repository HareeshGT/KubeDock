from themes import on_color
from .common import *

class FileEditorDialog(QDialog):
  """Remote file editor with find/replace, line numbers, and save-back."""

  def __init__(self, parent, sftp, ssh, remote_path: str, content=None, sudo_user=None):
    super().__init__(parent)
    self._sftp   = sftp
    self._ssh    = ssh
    self._remote  = remote_path
    self._sudo_user = sudo_user
    self._original = content or ""
    self._large_file = False
    self._matches  = []  # [(start, end), ...] offsets of current find matches
    self._match_idx = -1
    self._highlighter = None

    # When content is None, the editor opens immediately and streams
    # the file in live in the background (see _start_live_load) rather
    # than blocking behind a separate "downloading" dialog first.
    self._loading      = content is None
    self._load_worker    = None
    self._decoder      = None
    self._chunks_since_sync = 0
    self._buffer_small   = False
    self._pending_bytes   = None
    self._remote_size = None
    try:
      self._remote_size = int(self._sftp.stat(remote_path).st_size)
    except Exception:
      pass
    self._large_file = bool(self._remote_size and self._remote_size >= self.LARGE_FILE_BYTES)

    fname = os.path.basename(remote_path)
    self.setWindowTitle(f"Edit — {fname}")
    self.resize(1180, 760)
    self.setMinimumSize(900, 620)
    self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
    apply_qss_to(self)

    lay = QVBoxLayout(self)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    # ── Editor header ─────────────────────────────────────────
    header = QWidget()
    header.setObjectName("editor_header")
    header.setFixedHeight(64)
    header.setStyleSheet(
      f"QWidget#editor_header {{ background: {T['BG_PANEL']}; "
      f"border-bottom: 1px solid {T['BORDER']}; }}"
    )
    hl = QHBoxLayout(header)
    hl.setContentsMargins(16, 8, 16, 8)
    hl.setSpacing(10)

    file_badge = QFrame()
    file_badge.setFixedSize(38, 38)
    file_badge.setStyleSheet(
      f"QFrame {{ background: {_rgba(T['ACCENT'], 0.13)}; "
      f"border: 1px solid {_rgba(T['ACCENT'], 0.28)}; border-radius: 10px; }}"
    )
    file_badge_lay = QHBoxLayout(file_badge)
    file_badge_lay.setContentsMargins(0, 0, 0, 0)
    file_icon = QLabel()
    file_icon.setPixmap(icon_pixmap("file", color=T["ACCENT"], size=19))
    file_icon.setAlignment(Qt.AlignCenter)
    file_badge_lay.addWidget(file_icon)
    hl.addWidget(file_badge)

    title_col = QVBoxLayout()
    title_col.setSpacing(0)
    title_col.setContentsMargins(0, 1, 0, 1)

    name_row = QHBoxLayout()
    name_row.setSpacing(8)
    path_lbl = QLabel(fname)
    path_lbl.setStyleSheet(
      f"color: {T['TEXT_PRIMARY']}; font-size: 14px; font-weight: 700; background: transparent;"
    )
    path_lbl.setToolTip(remote_path)
    name_row.addWidget(path_lbl)

    self._modified_dot = QLabel("Modified")
    self._modified_dot.setStyleSheet(
      f"background: {_rgba(T['WARNING'], 0.16)}; color: {T['WARNING']}; "
      f"font-size: 10px; font-weight: 700; border: 1px solid {_rgba(T['WARNING'], 0.35)}; "
      f"border-radius: 8px; padding: 2px 7px;"
    )
    self._modified_dot.hide()
    name_row.addWidget(self._modified_dot)
    name_row.addStretch(1)
    title_col.addLayout(name_row)

    path_tail = remote_path
    if len(path_tail) > 110:
      path_tail = "…" + path_tail[-109:]
    path_detail = QLabel(path_tail)
    path_detail.setStyleSheet(
      f"color: {T['TEXT_MUTED']}; font-size: 11px; background: transparent;"
    )
    path_detail.setToolTip(remote_path)
    title_col.addWidget(path_detail)

    hl.addLayout(title_col, 1)

    remote_pill = QLabel("REMOTE")
    remote_pill.setAlignment(Qt.AlignCenter)
    remote_pill.setStyleSheet(
      f"background: {T['BG_ITEM']}; color: {T['TEXT_DIM']}; font-size: 10px; font-weight: 700; "
      f"letter-spacing: 0.8px; border: 1px solid {T['BORDER']}; border-radius: 8px; padding: 4px 8px;"
    )
    hl.addWidget(remote_pill)
    hl.addSpacing(6)

    self._save_close_btn = QCheckBox("Close after save")
    self._save_close_btn.setToolTip("Save the file and close the editor in one step")
    self._save_close_btn.setStyleSheet(
      f"QCheckBox {{ color: {T['TEXT_DIM']}; font-size: 11px; spacing: 6px; }}"
      f"QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 4px; border: 1px solid {T['BORDER']}; background: {T['BG_ITEM']}; }}"
      f"QCheckBox::indicator:checked {{ background: {T['ACCENT']}; border-color: {T['ACCENT']}; }}"
    )
    hl.addWidget(self._save_close_btn)

    discard_btn = QPushButton("Discard")
    discard_btn.setToolTip("Discard local changes and close")
    discard_btn.setFixedHeight(32)
    discard_btn.setObjectName("editor_ghost_danger")
    discard_btn.setStyleSheet(
      f"QPushButton#editor_ghost_danger {{ background: transparent; color: {T['DANGER']}; "
      f"border: 1px solid {_rgba(T['DANGER'], 0.4)}; border-radius: 8px; padding: 0 12px; }}"
      f"QPushButton#editor_ghost_danger:hover {{ background: {_rgba(T['DANGER'], 0.10)}; }}"
      f"QPushButton#editor_ghost_danger:pressed {{ background: {_rgba(T['DANGER'], 0.16)}; }}"
      f"QPushButton#editor_ghost_danger:disabled {{ color: {T['TEXT_MUTED']}; border-color: {T['BORDER']}; }}"
    )
    discard_btn.clicked.connect(self._confirm_close)
    hl.addWidget(discard_btn)

    save_btn = icon_button(" Save")
    save_btn.setObjectName("editor_primary")
    save_btn.setFixedHeight(32)
    save_btn.setMinimumWidth(92)
    save_btn.setToolTip("Save (Ctrl+S)")
    save_btn.clicked.connect(
      lambda: self._save_and_close() if self._save_close_btn.isChecked() else self._save()
    )
    save_btn.setStyleSheet(
      f"QPushButton#editor_primary {{ background: {T['ACCENT']}; color: {on_color(T['ACCENT'])}; border: 1px solid {T['ACCENT']}; "
      f"border-radius: 8px; padding: 0 14px; font-weight: 700; }}"
      f"QPushButton#editor_primary:hover {{ background: {T['ACCENT2']}; border-color: {T['ACCENT2']}; }}"
      f"QPushButton#editor_primary:pressed {{ background: {T['ACCENT']}; }}"
      f"QPushButton#editor_primary:disabled {{ background: {T['BG_ITEM']}; color: {T['TEXT_MUTED']}; border-color: {T['BORDER']}; }}"
    )
    hl.addWidget(save_btn)
    self._save_btn = save_btn

    lay.addWidget(header)

    # ── Compact command bar ───────────────────────────────────
    tools = QWidget()
    tools.setObjectName("editor_toolbar")
    tools.setFixedHeight(42)
    tools.setStyleSheet(
      f"QWidget#editor_toolbar {{ background: {T['BG_DARK']}; border-bottom: 1px solid {T['BORDER']}; }}"
    )
    tl = QHBoxLayout(tools)
    tl.setContentsMargins(12, 5, 12, 5)
    tl.setSpacing(5)

    def _tool_btn(label, tooltip, width=34, checkable=False):
      b = QPushButton(label)
      b.setFixedSize(width, 30)
      b.setCheckable(checkable)
      b.setToolTip(tooltip)
      b.setStyleSheet(
        f"QPushButton {{ background: transparent; color: {T['TEXT_DIM']}; border: 1px solid transparent; border-radius: 7px; padding: 0 8px; }}"
        f"QPushButton:hover {{ background: {T['BG_ITEM']}; color: {T['TEXT_PRIMARY']}; border-color: {T['BORDER']}; }}"
        f"QPushButton:checked {{ background: {_rgba(T['ACCENT'], 0.14)}; color: {T['ACCENT2']}; border-color: {_rgba(T['ACCENT'], 0.38)}; }}"
      )
      return b

    def _separator():
      sep = QFrame()
      sep.setFrameShape(QFrame.VLine)
      sep.setFixedHeight(20)
      sep.setStyleSheet(f"color: {T['BORDER']};")
      return sep

    undo_btn = _tool_btn("↶", "Undo (Ctrl+Z)")
    undo_btn.clicked.connect(lambda: self.editor.undo())
    tl.addWidget(undo_btn)
    redo_btn = _tool_btn("↷", "Redo (Ctrl+Shift+Z)")
    redo_btn.clicked.connect(lambda: self.editor.redo())
    tl.addWidget(redo_btn)
    tl.addWidget(_separator())

    self._find_btn = icon_button(" Find")
    self._find_btn.setCheckable(True)
    self._find_btn.setFixedHeight(30)
    self._find_btn.setMinimumWidth(78)
    self._find_btn.setToolTip("Find / Replace (Ctrl+F)")
    self._find_btn.toggled.connect(self._toggle_find_bar)
    tl.addWidget(self._find_btn)

    self._wrap_btn = _tool_btn("↔", "Wrap lines", width=72, checkable=True)
    self._wrap_btn.setChecked(True)
    self._wrap_btn.toggled.connect(self._toggle_wrap)
    tl.addWidget(self._wrap_btn)

    tl.addStretch(1)

    view_lbl = QLabel("VIEW")
    view_lbl.setStyleSheet(
      f"color: {T['TEXT_MUTED']}; font-size: 9px; font-weight: 700; letter-spacing: 1px; padding-right: 2px;"
    )
    tl.addWidget(view_lbl)

    zoom_frame = QFrame()
    zoom_frame.setObjectName("zoom_group")
    zoom_frame.setStyleSheet(
      f"QFrame#zoom_group {{ background: {T['BG_ITEM']}; border: 1px solid {T['BORDER']}; border-radius: 8px; }}"
    )
    zf = QHBoxLayout(zoom_frame)
    zf.setContentsMargins(2, 2, 2, 2)
    zf.setSpacing(0)

    zoom_out_btn = _tool_btn("−", "Zoom out (Ctrl+-)", width=30)
    zoom_out_btn.clicked.connect(lambda: (self.editor.zoom_out(), self._update_zoom_label()))
    zf.addWidget(zoom_out_btn)

    self._zoom_lbl = QLabel("100%")
    self._zoom_lbl.setFixedWidth(44)
    self._zoom_lbl.setAlignment(Qt.AlignCenter)
    self._zoom_lbl.setStyleSheet(
      f"color: {T['TEXT_PRIMARY']}; font-size: 11px; font-weight: 600; background: transparent;"
    )
    self._zoom_lbl.setToolTip("Reset zoom (Ctrl+0)")
    zf.addWidget(self._zoom_lbl)

    zoom_in_btn = _tool_btn("+", "Zoom in (Ctrl++)", width=30)
    zoom_in_btn.clicked.connect(lambda: (self.editor.zoom_in(), self._update_zoom_label()))
    zf.addWidget(zoom_in_btn)
    tl.addWidget(zoom_frame)
    lay.addWidget(tools)

    # Thin loading strip.
    self._load_bar = QProgressBar()
    self._load_bar.setFixedHeight(3)
    self._load_bar.setTextVisible(False)
    self._load_bar.setStyleSheet(
      f"QProgressBar {{ border: none; background: {T['BG_ITEM']}; }}"
      f"QProgressBar::chunk {{ background: {T['ACCENT']}; }}"
    )
    self._load_bar.hide()
    lay.addWidget(self._load_bar)

    # ── Find/Replace bar ──────────────────────────────────
    self._find_bar = QWidget()
    self._find_bar.setFixedHeight(50)
    self._find_bar.setStyleSheet(
      f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
    )
    fb = QHBoxLayout(self._find_bar)
    fb.setContentsMargins(12, 8, 12, 8)
    fb.setSpacing(6)

    input_style = (
      f"QLineEdit {{ background: {T['BG_DARK']}; border: 1px solid {T['BORDER']}; "
      f"border-radius: 7px; padding: 5px 10px; }}"
      f"QLineEdit:focus {{ border-color: {T['ACCENT']}; }}"
    )

    self._find_inp = QLineEdit()
    self._find_inp.setPlaceholderText("Find…")
    self._find_inp.setFixedWidth(220)
    self._find_inp.setStyleSheet(input_style)
    self._find_inp.textChanged.connect(self._do_highlight)
    self._find_inp.returnPressed.connect(self._on_find_enter)
    fb.addWidget(self._find_inp)

    self._match_lbl = QLabel("")
    self._match_lbl.setFixedWidth(64)
    self._match_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
    fb.addWidget(self._match_lbl)

    for text, tip, slot in [
      ("↑", "Previous match (Shift+Enter)", self._find_prev),
      ("↓", "Next match (Enter)",      self._find_next),
    ]:
      b = QPushButton(text)
      b.setFixedSize(30, 30)
      b.setToolTip(tip)
      b.clicked.connect(slot)
      fb.addWidget(b)

    fb.addSpacing(6)
    divider = QFrame()
    divider.setFrameShape(QFrame.VLine)
    divider.setStyleSheet(f"color: {T['BORDER']};")
    fb.addWidget(divider)
    fb.addSpacing(6)

    self._replace_inp = QLineEdit()
    self._replace_inp.setPlaceholderText("Replace…")
    self._replace_inp.setMinimumWidth(190)
    self._replace_inp.setStyleSheet(input_style)
    fb.addWidget(self._replace_inp)

    for label, slot in [
      ("Replace",   self._replace_one),
      ("Replace All", self._replace_all),
    ]:
      b = QPushButton(label)
      b.setFixedHeight(30)
      b.setFixedWidth(96 if "All" in label else 70)
      b.clicked.connect(slot)
      fb.addWidget(b)

    fb.addStretch()

    self._case_chk = QCheckBox("Aa")
    self._case_chk.setToolTip("Match case")
    self._case_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
    self._case_chk.stateChanged.connect(self._do_highlight)
    fb.addWidget(self._case_chk)

    self._regex_chk = QCheckBox(".*")
    self._regex_chk.setToolTip("Regular expression")
    self._regex_chk.setStyleSheet(f"color: {T['TEXT_DIM']}; padding-left: 6px;")
    self._regex_chk.stateChanged.connect(self._do_highlight)
    fb.addWidget(self._regex_chk)

    close_find_btn = icon_button("")
    close_find_btn.setFixedSize(26, 26)
    close_find_btn.setToolTip("Close (Esc)")
    close_find_btn.clicked.connect(lambda: self._find_btn.setChecked(False))
    fb.addWidget(close_find_btn)

    self._find_bar.hide()
    lay.addWidget(self._find_bar)

    # ── Editor area ───────────────────────────────────────
    editor_frame = QFrame()
    editor_frame.setObjectName("editor_surface")
    editor_frame.setStyleSheet(
      f"QFrame#editor_surface {{ background: {T['BG_DARK']}; border: 1px solid {T['BORDER']}; }}"
    )
    editor_lay = QVBoxLayout(editor_frame)
    editor_lay.setContentsMargins(0, 0, 0, 0)
    editor_lay.setSpacing(0)

    # Monaco provides the VS Code-style editing surface when Qt WebEngine
    # is available. Keep the native editor as a compatibility fallback so
    # missing WebEngine never prevents the remote file editor from opening.
    if MonacoEditor is not None and MonacoEditor.available():
      self.editor = MonacoEditor(base_point_size=12, large_file=self._large_file)
      self.editor.set_filename(fname)
      self.editor.setPlainText(content or "")
    else:
      self.editor = CodeEditor(base_point_size=12)
      self.editor.setPlainText(content or "")
    self.editor.textChanged.connect(self._on_text_changed)
    if hasattr(self.editor, "cursorPositionChanged"):
      self.editor.cursorPositionChanged.connect(self._update_cursor_pos)
    editor_lay.addWidget(self.editor, 1)
    lay.addWidget(editor_frame, 1)

    # Syntax highlighting is handled by Monaco when it is active. The
    # native editor keeps the existing Qt highlighter as its fallback.
    if self._large_file:
      self._find_btn.setEnabled(False)
      self._find_btn.setToolTip("Find/Replace is disabled for large-file memory mode")
    if isinstance(self.editor, CodeEditor):
      self._highlighter, self._lang = make_highlighter(self.editor.document(), fname)
    else:
      self._highlighter, self._lang = None, None
    # ── Status bar ─────────────────────────────────────────
    sb_widget = QWidget()
    sb_widget.setObjectName("editor_status")
    sb_widget.setFixedHeight(30)
    sb_widget.setStyleSheet(
      f"QWidget#editor_status {{ background: {T['BG_PANEL']}; border-top: 1px solid {T['BORDER']}; }}"
    )
    sb = QHBoxLayout(sb_widget)
    sb.setContentsMargins(14, 0, 14, 0)
    sb.setSpacing(12)

    self._status_lbl = QLabel("Ready")
    self._status_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 11px; font-weight: 600;")
    sb.addWidget(self._status_lbl)

    status_divider = QFrame()
    status_divider.setFrameShape(QFrame.VLine)
    status_divider.setFixedHeight(14)
    status_divider.setStyleSheet(f"color: {T['BORDER']};")
    sb.addWidget(status_divider)
    sb.addStretch()

    self._cursor_lbl = QLabel("Ln 1, Col 1")
    self._cursor_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 11px;")
    sb.addWidget(self._cursor_lbl)

    for badge_text in ("UTF-8", "LF"):
      badge = QLabel(badge_text)
      badge.setStyleSheet(
        f"color: {T['TEXT_MUTED']}; font-size: 10px; font-weight: 600; "
        f"background: {T['BG_ITEM']}; border: 1px solid {T['BORDER']}; "
        f"border-radius: 6px; padding: 2px 6px;"
      )
      sb.addWidget(badge)

    ext = os.path.splitext(fname)[1].lower()
    lang_lbl = QLabel(LANG_LABEL.get(self._lang, ext.lstrip(".").upper() if ext else "Plain Text"))
    lang_lbl.setStyleSheet(
      f"color: {T['ACCENT2']}; font-size: 10px; font-weight: 700; "
      f"background: {_rgba(T['ACCENT2'], 0.10)}; border: 1px solid {_rgba(T['ACCENT2'], 0.28)}; "
      f"border-radius: 6px; padding: 2px 7px;"
    )
    sb.addWidget(lang_lbl)
    lay.addWidget(sb_widget)
    self.editor.cursorPositionChanged.connect(self._update_cursor_pos)

    QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self._save)
    QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(self._open_find)
    if hasattr(self.editor, "findRequested"):
      self.editor.findRequested.connect(self._open_find)
    QShortcut(QKeySequence("Escape"), self._find_bar).activated.connect(
      lambda: self._find_btn.setChecked(False)
    )
    QShortcut(QKeySequence("Shift+Return"), self._find_inp).activated.connect(self._find_prev)
    QShortcut(QKeySequence("Ctrl++"), self).activated.connect(
      lambda: (self.editor.zoom_in(), self._update_zoom_label()))
    QShortcut(QKeySequence("Ctrl+="), self).activated.connect(
      lambda: (self.editor.zoom_in(), self._update_zoom_label()))
    QShortcut(QKeySequence("Ctrl+-"), self).activated.connect(
      lambda: (self.editor.zoom_out(), self._update_zoom_label()))
    QShortcut(QKeySequence("Ctrl+0"), self).activated.connect(
      lambda: (self.editor.zoom_reset(), self._update_zoom_label()))

    if self._loading:
      self._start_live_load()

  # ── Helpers ───────────────────────────────────────────────
  # Line numbers are painted directly into CodeEditor's own gutter (see
  # editor_widgets.py) and repaint themselves automatically on every
  # block-count/scroll change — nothing to keep in sync here anymore.
  def _update_zoom_label(self):
    pct = round(self.editor._pt / self.editor._base_pt * 100)
    self._zoom_lbl.setText(f"{pct}%")

  def _toggle_wrap(self, on: bool):
    self.editor.setLineWrapMode(QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap)

  def _editor_selection(self) -> str:
    # Both editors: Monaco mirrors its selection; CodeEditor uses Qt's cursor.
    if hasattr(self.editor, "selected_text"):
      txt = self.editor.selected_text()
    else:
      txt = self.editor.textCursor().selectedText().replace("\u2029", "\n")
    return txt

  def _seed_find_from_selection(self):
    # Selection wins over the previous query. Multiline selections are
    # skipped: the Find field is single-line, so keep existing semantics.
    sel = self._editor_selection()
    if sel and "\n" not in sel:
      self._find_inp.setText(sel)

  def _open_find(self):
    # Cmd/Ctrl+F entry point. If the bar is closed, the toggle handler
    # opens it (and seeds it); if already open, just re-seed and refocus.
    if self._find_btn.isChecked():
      self._seed_find_from_selection()
      self._find_inp.setFocus()
      self._find_inp.selectAll()
      if self._find_inp.text():
        self._do_highlight()
    else:
      self._find_btn.setChecked(True)

  def _toggle_find_bar(self, on: bool):
    if on and self._loading:
      # Ctrl+F can still fire this while a big file is mid-stream (the
      # button itself is disabled, but the shortcut isn't gated on that).
      # Matches computed against a partially-loaded document are wrong
      # anyway, so just bounce it back off instead of opening the bar.
      self._find_btn.setChecked(False)
      return
    self._find_bar.setVisible(on)
    if on:
      self._seed_find_from_selection()
      self._find_inp.setFocus()
      self._find_inp.selectAll()
      if self._find_inp.text():
        self._do_highlight()
    else:
      self._clear_highlights()

  def _on_text_changed(self):
    if self._large_file:
      self._modified_dot.show()
      return
    self._modified_dot.setVisible(self.editor.toPlainText() != self._original)

  def _update_cursor_pos(self):
    cur = self.editor.textCursor()
    ln = cur.blockNumber() + 1
    col = cur.columnNumber() + 1
    self._cursor_lbl.setText(f"Ln {ln}, Col {col}")

  def _set_status(self, msg, color=None):
    # Delayed "Ready" resets (QTimer.singleShot) can fire after the dialog
    # was closed and its widgets deleted; PyQt5 aborts on that exception.
    try:
      self._status_lbl.setText(msg)
      self._status_lbl.setStyleSheet(
        f"color: {color or T['TEXT_MUTED']}; font-size: 13px;"
      )
    except RuntimeError:
      pass

  # ── Live streaming load ─────────────────────────────────────
  # The editor opens immediately (empty) and content is appended as it
  # streams in from FileStreamReadWorker, instead of blocking behind a
  # separate "downloading" dialog first — you can start reading/
  # scrolling the file as soon as the first chunks land. Save stays
  # disabled until the whole file has arrived, so there's no risk of
  # saving back a partially-loaded file.
  # Files at or under this size are buffered fully in memory and dropped
  # into the editor in one shot instead of being appended chunk-by-chunk
  # (see _on_load_chunk / _on_load_finished). Streaming was previously
  # unconditional — every file, however small, opened behind a "Loading…"
  # spinner and a stream of incremental JS calls into Monaco meant purely
  # for files too big to read in one go. For a small file that's just
  # visible lag for no benefit, and firing several of those JS round-trips
  # back-to-back (each one replacing the whole Monaco model) right before
  # the user starts typing into Find was what made Find crash the app.
  SMALL_FILE_BYTES = 256 * 1024

  # At/above this size the editor runs in "large-file mode": no full-text
  # shadow QTextDocument, no per-edit full-document sync to Python (text is
  # pulled on demand for Save), and Monaco's whole-document features off.
  # In normal mode every keystroke ships the entire document across the
  # QWebChannel and rebuilds the shadow document, which measured ~0.09s/MB
  # (~3s and +360MB per keystroke at 30MB) — hence this is well below
  # MAX_EDIT_BYTES.
  LARGE_FILE_BYTES = 8 * 1024 * 1024

  def _start_live_load(self):
    self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    self._chunks_since_sync = 0
    self._save_btn.setEnabled(False)
    self._save_close_btn.setEnabled(False)
    self._find_btn.setEnabled(False)

    # Assume small until proven otherwise, and find out reactively from
    # data the *background* worker reports (chunk_ready / progress),
    # rather than statting the file up front here. An earlier version of
    # this called self._sftp.stat(...) directly in this method — which
    # runs on the GUI thread — right before starting the worker. Right
    # after closing a previous editor, that previous worker's QThread
    # can still be winding down (cancel() only closes its handle, it
    # doesn't wait() for the thread to exit), so a synchronous stat()
    # call here from the GUI thread could contend with it for the same
    # SFTP/SSH connection, which isn't safe to use from two threads at
    # once — that's what turned a quick close-then-reopen of the same
    # file into a long/hung "Loading…". Nothing here now touches the
    # connection except the worker itself, on its own thread.
    self._buffer_small = True
    self._pending_bytes = bytearray()
    self._load_bar.hide()

    # Undo/redo would otherwise record every one of the ~240 chunk
    # inserts that make up a big file's load as its own undo command —
    # each holding a copy of the text it inserted, so the undo stack
    # alone would double memory use for as long as the dialog stays
    # open. Off during the bulk load, back on once it's real editing.
    self.editor.setUndoRedoEnabled(False)

    # The modified-dot recompute (toPlainText() == original comparison)
    # is O(n) — doing it on every single 64KB chunk of a big file would
    # add up fast, so it's skipped entirely during the load and only
    # reinstated once the whole file has arrived.
    try:
      self.editor.textChanged.disconnect(self._on_text_changed)
    except Exception:
      pass

    self._load_worker = FileStreamReadWorker(
      self._sftp, self._ssh, self._remote,
      sudo_user=self._sudo_user, max_bytes=self.MAX_EDIT_BYTES,
    )
    self._load_worker.chunk_ready.connect(self._on_load_chunk)
    self._load_worker.progress.connect(self._on_load_progress)
    self._load_worker.finished_ok.connect(self._on_load_finished)
    self._load_worker.finished_err.connect(self._on_load_error)
    self._load_worker.start()

  def _flush_small_buffer(self):
    """Switch from buffering to live-append mode mid-load.

    Called once we learn (from data the background worker has already
    sent us) that the file is bigger than SMALL_FILE_BYTES after all.
    Pushes what's been buffered so far into the editor as one chunk,
    then lets subsequent chunks flow through the normal live-append path.
    """
    if not self._buffer_small:
      return
    self._buffer_small = False
    self._load_bar.show()
    self._load_bar.setRange(0, 0)
    self._set_status("Loading…", T['WARNING'])
    try:
      text = self._decoder.decode(bytes(self._pending_bytes))
    except RuntimeError:
      text = ""
    self._pending_bytes = bytearray()
    if text:
      if hasattr(self.editor, "append_text"):
        self.editor.append_text(text)
      else:
        cur = self.editor.textCursor()
        cur.movePosition(QTextCursor.End)
        cur.insertText(text)

  def _on_load_chunk(self, chunk: bytes):
    if self._buffer_small:
      self._pending_bytes += chunk
      if len(self._pending_bytes) > self.SMALL_FILE_BYTES:
        self._flush_small_buffer()
      return
    try:
      text = self._decoder.decode(chunk)
    except RuntimeError:
      return # dialog is mid-teardown; drop the chunk
    if not text:
      return
    if hasattr(self.editor, "append_text"):
      # Monaco has a browser-side model plus a QTextDocument shadow model.
      # Always use the editor's append API during streaming so both stay
      # synchronized. Writing only to the shadow model makes an existing
      # remote file appear as a brand-new/empty file in Monaco.
      self.editor.append_text(text)
    else:
      cur = self.editor.textCursor()
      cur.movePosition(QTextCursor.End)
      cur.insertText(text)
    self._chunks_since_sync += 1

  def _on_load_progress(self, done, total):
    # A known total bigger than the small-file cutoff means this isn't
    # going to fit in one buffered shot — flip into live-streaming mode
    # right away instead of waiting for the buffer itself to cross the
    # threshold, so the loading UI shows up promptly for a big file.
    if self._buffer_small and total > self.SMALL_FILE_BYTES:
      self._flush_small_buffer()
    if self._buffer_small:
      return
    try:
      if total > 0:
        self._load_bar.setRange(0, 1000)
        self._load_bar.setValue(int(done / total * 1000))
        self._set_status(
          "Loading… {} / {}".format(size_fmt(done), size_fmt(total)), T['WARNING'])
      else:
        self._load_bar.setRange(0, 0)
        self._set_status("Loading… {}".format(size_fmt(done)), T['WARNING'])
    except RuntimeError:
      pass

  def _on_load_finished(self, total_bytes: int):
    try:
      if self._buffer_small:
        # File turned out to be small end-to-end — render it in one
        # shot rather than replaying the incremental-append path.
        full_text = self._pending_bytes.decode("utf-8", errors="replace")
        self._pending_bytes = bytearray()
        self.editor.setPlainText(full_text)
      else:
        tail = self._decoder.decode(b"", final=True)
        if tail:
          if hasattr(self.editor, "append_text"):
            self.editor.append_text(tail)
          else:
            cur = self.editor.textCursor()
            cur.movePosition(QTextCursor.End)
            cur.insertText(tail)
      if total_bytes >= self.MAX_EDIT_BYTES:
        truncation = (
          "\n\n[... file truncated at {} -- too large to fully load "
          "into the editor. Download it instead to view the whole "
          "thing.]".format(size_fmt(self.MAX_EDIT_BYTES))
        )
        if hasattr(self.editor, "append_text"):
          self.editor.append_text(truncation)
        else:
          cur = self.editor.textCursor()
          cur.movePosition(QTextCursor.End)
          cur.insertText(truncation)
      self._loading = False
      self._load_bar.hide()
      self.editor.setUndoRedoEnabled(True)
      self._save_btn.setEnabled(True)
      self._save_close_btn.setEnabled(True)
      self._find_btn.setEnabled(True)
      self._original = self.editor.toPlainText()
      self._modified_dot.hide()
      self._set_status("Ready")
      self.editor.textChanged.connect(self._on_text_changed)
    except RuntimeError:
      pass
    finally:
      # Drop the worker/decoder now that loading is done rather than
      # holding them (and whatever buffers paramiko still has open)
      # alive for the rest of the dialog's lifetime.
      self._load_worker = None
      self._decoder   = None
      self._pending_bytes = None

  def _on_load_error(self, msg):
    try:
      self._loading = False
      self._load_bar.hide()
      self.editor.setUndoRedoEnabled(True)
      self._find_btn.setEnabled(True)
      self._set_status("Failed to load: {}".format(msg), T['DANGER'])
      self.editor.textChanged.connect(self._on_text_changed)
    except RuntimeError:
      pass
    finally:
      self._load_worker = None
      self._decoder   = None
      self._pending_bytes = None
    QMessageBox.critical(self, "Cannot Open File", msg)

  # ── Find / Replace ────────────────────────────────────────
  # Matches are tracked as a plain list of (start, end) character offsets
  # computed with Python's `re` over the document's full text — this
  # gives regex support and an accurate "3 of 17" counter/navigation for
  # free, and (unlike QTextDocument.find + mergeCharFormat) the matches
  # are painted purely as ExtraSelections, so they never touch the
  # document's real character formatting and can't clash with the
  # syntax highlighter's colours.
  def _compiled_pattern(self):
    term = self._find_inp.text()
    if not term:
      return None
    flags = 0 if self._case_chk.isChecked() else re.IGNORECASE
    try:
      if self._regex_chk.isChecked():
        return re.compile(term, flags)
      return re.compile(re.escape(term), flags)
    except re.error:
      return None

  def _do_highlight(self):
    pattern = self._compiled_pattern()
    self._matches  = []
    self._match_idx = -1
    if pattern is None:
      self.editor.set_search_selections([])
      if self._find_inp.text() and self._regex_chk.isChecked():
        self._match_lbl.setText("bad regex")
        self._match_lbl.setStyleSheet(f"color: {T['DANGER']}; font-size: 12px;")
      else:
        self._match_lbl.setText("")
        self._match_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
      return

    text = self.editor.toPlainText()
    self._matches = [(m.start(), m.end()) for m in pattern.finditer(text)]

    # Keep navigation anchored near the cursor rather than always
    # snapping back to match #1 on every keystroke.
    cur_pos = self.editor.textCursor().position()
    self._match_idx = next(
      (i for i, (s, _e) in enumerate(self._matches) if s >= cur_pos), 0
    ) if self._matches else -1

    self._apply_match_selections()
    self._update_match_label()

  def _apply_match_selections(self):
    doc = self.editor.document()
    selections = []
    normal_fmt = QTextCharFormat()
    normal_fmt.setBackground(QColor(T['WARNING']))
    normal_fmt.setForeground(QColor("#1a1a1a"))
    current_fmt = QTextCharFormat()
    current_fmt.setBackground(QColor(T['ACCENT']))
    current_fmt.setForeground(QColor("#ffffff"))

    for i, (start, end) in enumerate(self._matches):
      sel = QTextEdit.ExtraSelection()
      sel.format = current_fmt if i == self._match_idx else normal_fmt
      c = QTextCursor(doc)
      c.setPosition(start)
      c.setPosition(end, QTextCursor.KeepAnchor)
      sel.cursor = c
      selections.append(sel)
    self.editor.set_search_selections(selections)

  def _update_match_label(self):
    if not self._matches:
      self._match_lbl.setText("No results" if self._find_inp.text() else "")
    else:
      self._match_lbl.setText(f"{self._match_idx + 1} of {len(self._matches)}")
    self._match_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")

  def _goto_match(self, idx):
    if not self._matches:
      return
    self._match_idx = idx % len(self._matches)
    start, end = self._matches[self._match_idx]
    c = self.editor.textCursor()
    c.setPosition(start)
    c.setPosition(end, QTextCursor.KeepAnchor)
    self.editor.setTextCursor(c)
    self.editor.ensureCursorVisible()
    self._apply_match_selections()
    self._update_match_label()

  def _on_find_enter(self):
    # Enter = next match, Shift+Enter = previous match
    if QApplication.keyboardModifiers() & Qt.ShiftModifier:
      self._find_prev()
    else:
      self._find_next()

  def _find_next(self):
    if not self._matches:
      self._do_highlight()
    if self._matches:
      self._goto_match(self._match_idx + 1 if self._match_idx >= 0 else 0)

  def _find_prev(self):
    if not self._matches:
      self._do_highlight()
    if self._matches:
      self._goto_match(self._match_idx - 1 if self._match_idx >= 0 else -1)

  def _clear_highlights(self):
    self._matches  = []
    self._match_idx = -1
    self.editor.set_search_selections([])
    self._match_lbl.setText("")

  def _replace_one(self):
    if not self._matches:
      self._do_highlight()
    if not self._matches or self._match_idx < 0:
      return
    start, end = self._matches[self._match_idx]
    c = self.editor.textCursor()
    c.setPosition(start)
    c.setPosition(end, QTextCursor.KeepAnchor)
    if hasattr(self.editor, "replace_selection"):
      self.editor.replace_selection(self._replace_inp.text())
    else:
      c.insertText(self._replace_inp.text())
    self._do_highlight()

  def _replace_all(self):
    pattern = self._compiled_pattern()
    replace = self._replace_inp.text()
    if pattern is None:
      return
    text = self.editor.toPlainText()
    try:
      new_text, n = pattern.subn(
        replace if self._regex_chk.isChecked() else replace.replace("\\", "\\\\"),
        text,
      )
    except re.error as e:
      self._set_status(f"Replace failed: {e}", T['DANGER'])
      return
    if n:
      cur = self.editor.textCursor()
      pos = cur.position()
      self.editor.setPlainText(new_text)
      cur = self.editor.textCursor()
      cur.setPosition(min(pos, len(new_text)))
      self.editor.setTextCursor(cur)
      self._set_status(f"Replaced {n} occurrence{'s' if n != 1 else ''}", T['SUCCESS'])
      QTimer.singleShot(2000, lambda: self._set_status("Ready"))
    self._do_highlight()

  # ── Save ──────────────────────────────────────────────────
  def _save(self) -> bool:
    if self._loading:
      self._set_status("Still loading — please wait", T['WARNING'])
      return False
    if self._large_file:
      self._set_status("Preparing save…", T["WARNING"])
      self.editor.get_text(self._save_large_content)
      return True
    content = self.editor.toPlainText()
    self._set_status("Saving…", T['WARNING'])
    try:
      data = content.encode("utf-8")
      buf = io.BytesIO(data)
      if self._sudo_user:
        tmp = f"/tmp/.ec2mgr_edit_{os.getpid()}"
        self._sftp._sftp.putfo(io.BytesIO(data), tmp)
        code, out, err = self._sftp._run(
          f"sudo mv {self._sftp._sq(tmp)} {self._sftp._sq(self._remote)} "
          f"&& sudo chown {self._sudo_user} {self._sftp._sq(self._remote)}"
        )
        if code != 0:
          raise PermissionError((err or out or "sudo save failed").strip())
      else:
        if hasattr(self._sftp, "_ftp"):
          self._sftp.putfo(buf, self._remote)
        else:
          self._sftp._sftp.putfo(buf, self._remote)
      self._original = content
      self._modified_dot.hide()
      self._set_status("Saved ", T['SUCCESS'])
      QTimer.singleShot(2000, lambda: self._set_status("Ready"))
      return True
    except Exception as e:
      self._set_status(f"Save failed: {e}", T['DANGER'])
      QMessageBox.critical(self, "Save Failed", str(e))
      return False

  def _save_large_content(self, content):
    try:
      data = (content or "").encode("utf-8")
      buf = io.BytesIO(data)
      self._set_status("Saving…", T["WARNING"])
      if self._sudo_user:
        tmp = f"/tmp/.ec2mgr_edit_{os.getpid()}"
        self._sftp._sftp.putfo(buf, tmp)
        code, out, err = self._sftp._run(
          f"sudo mv {self._sftp._sq(tmp)} {self._sftp._sq(self._remote)} "
          f"&& sudo chown {self._sudo_user} {self._sftp._sq(self._remote)}"
        )
        if code != 0:
          raise PermissionError((err or out or "sudo save failed").strip())
      elif hasattr(self._sftp, "_ftp"):
        self._sftp.putfo(buf, self._remote)
      else:
        self._sftp._sftp.putfo(buf, self._remote)
      self._modified_dot.hide()
      self._set_status("Saved", T["SUCCESS"])
      QTimer.singleShot(2000, lambda: self._set_status("Ready"))
    except Exception as e:
      self._set_status(f"Save failed: {e}", T["DANGER"])
      QMessageBox.critical(self, "Save Failed", str(e))
  def _save_and_close(self):
    if self._large_file:
      self._set_status("Preparing save…", T["WARNING"])
      self.editor.get_text(self._save_large_content_and_close)
      return
    if self._save():
      self.accept()

  def _save_large_content_and_close(self, content):
    try:
      data = (content or "").encode("utf-8")
      buf = io.BytesIO(data)
      self._set_status("Saving…", T["WARNING"])
      if self._sudo_user:
        tmp = f"/tmp/.ec2mgr_edit_{os.getpid()}"
        self._sftp._sftp.putfo(buf, tmp)
        code, out, err = self._sftp._run(
          f"sudo mv {self._sftp._sq(tmp)} {self._sftp._sq(self._remote)} "
          f"&& sudo chown {self._sudo_user} {self._sftp._sq(self._remote)}"
        )
        if code != 0:
          raise PermissionError((err or out or "sudo save failed").strip())
      elif hasattr(self._sftp, "_ftp"):
        self._sftp.putfo(buf, self._remote)
      else:
        self._sftp._sftp.putfo(buf, self._remote)
      self.accept()
    except Exception as e:
      self._set_status(f"Save failed: {e}", T["DANGER"])
      QMessageBox.critical(self, "Save Failed", str(e))
  def _cancel_live_load(self):
    """Safely stop the live-load worker during dialog teardown.

    QThread calls deleteLater() when it finishes. That means the Python
    attribute can briefly outlive the underlying QObject. Never touch
    signals or methods on a wrapper whose C++ object has already gone.
    """
    worker = self._load_worker
    self._load_worker = None

    if worker is None:
      self._loading = False
      return

    if sip is not None:
      try:
        if sip.isdeleted(worker):
          self._loading = False
          return
      except Exception:
        pass

    try:
      for sig, slot in (
        (worker.chunk_ready, self._on_load_chunk),
        (worker.progress, self._on_load_progress),
        (worker.finished_ok, self._on_load_finished),
        (worker.finished_err, self._on_load_error),
      ):
        try:
          sig.disconnect(slot)
        except (TypeError, RuntimeError):
          pass

      try:
        worker.cancel()
      except RuntimeError:
        pass
    except RuntimeError:
      # Qt can destroy the worker between the lifetime check and the
      # first signal/method access. Closing the dialog must still win.
      pass
    finally:
      self._loading = False

  def _confirm_close(self):
    if self._loading:
      self._cancel_live_load()
      self.reject()
      return
    if self.editor.toPlainText() != self._original:
      r = QMessageBox.question(
        self, "Discard Changes",
        "You have unsaved changes. Discard and close?",
        QMessageBox.Discard | QMessageBox.Cancel,
      )
      if r != QMessageBox.Discard:
        return
    self.reject()

  def keyPressEvent(self, event):
    # QLineEdit ignores Enter/Return after emitting returnPressed, so the
    # key used to bubble up to QDialog, which "clicks" the default button
    # (Save/Discard) and closed the editor when pressing Enter in Find.
    # Swallow it for the dialog's text fields; returnPressed already runs
    # the find action.
    if event.key() in (Qt.Key_Return, Qt.Key_Enter):
      if isinstance(QApplication.focusWidget(), QLineEdit):
        event.accept()
        return
    super().keyPressEvent(event)

  def closeEvent(self, event):
    if self._loading:
      self._cancel_live_load()
      try:
        if hasattr(self.editor, "dispose"):
          self.editor.dispose()
      except Exception:
        pass
      event.accept()
      return
    if self.editor.toPlainText() != self._original:
      r = QMessageBox.question(
        self, "Unsaved Changes", "Discard changes and close?",
        QMessageBox.Discard | QMessageBox.Cancel,
      )
      if r != QMessageBox.Discard:
        event.ignore()
        return
    event.accept()

  MAX_EDIT_BYTES = 100 * 1024 * 1024

  @classmethod
  def open_remote(cls, parent, sftp, ssh, remote_path: str, sudo_user=None):
    """Opens *remote_path* for editing immediately — the editor window
    appears right away and the file's content streams in live from a
    background FileStreamReadWorker (see _start_live_load), instead of
    blocking behind a separate "downloading" dialog first. You can
    start reading/scrolling as soon as the first chunks land; Save
    stays disabled until the whole file has arrived.
    """
    dlg = cls(parent, sftp, ssh, remote_path, content=None, sudo_user=sudo_user)
    dlg.exec_()
    dlg._release_resources()
    return dlg

  def _release_resources(self):
    """Free the editor after the dialog closes. The dialog is parented to
    the main window and nothing else holds it, so without this every closed
    editor kept its Monaco page (the whole document), the shadow
    QTextDocument and ``_original`` alive until the app exited."""
    self._original = ""
    try:
      if hasattr(self.editor, "dispose"):
        self.editor.dispose()
    except Exception:
      pass
    self.deleteLater()