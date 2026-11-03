from .common import *

from .ai_explain import AIExplainDialog

class ContainerPickerDialog(QDialog):
  """Container selector used before Exec or Logs when a pod has
  multiple containers."""

  def __init__(
    self,
    parent,
    pod: str,
    containers: list,
    action: str = "Exec"
  ):
    super().__init__(parent)

    self._action = action

    self.setWindowTitle(f"Select Container — {pod}")
    apply_qss_to(self)
    self.setMinimumWidth(340)

    layout = QVBoxLayout(self)
    layout.setContentsMargins(16, 16, 16, 16)
    layout.setSpacing(10)

    info = QLabel(
      f'"{pod}" has {len(containers)} containers — '
      f'pick one to {self._action.lower()}:'
    )
    info.setWordWrap(True)
    info.setStyleSheet(
      f"color: {T['TEXT_DIM']}; font-size: 12px;"
    )
    layout.addWidget(info)

    self.combo = QComboBox()
    self.combo.addItems(containers)
    layout.addWidget(self.combo)

    bb = QDialogButtonBox(
      QDialogButtonBox.Ok | QDialogButtonBox.Cancel
    )

    ok_btn = bb.button(QDialogButtonBox.Ok)
    ok_btn.setObjectName("primary")
    ok_btn.setText(self._action)

    bb.accepted.connect(self.accept)
    bb.rejected.connect(self.reject)
    layout.addWidget(bb)

  def selected_container(self) -> str:
    return self.combo.currentText()


class ExecDialog(QDialog):
  """Live interactive shell inside a pod.

  One persistent ``kubectl exec -it`` session (see PodExecStreamWorker) is
  kept open, and its output is rendered as it arrives — so a long-running
  download/install shows live progress instead of appearing only once the
  command finishes. ``cd``, env vars, pipes etc. just work because it is a
  real shell. Full-screen programs (vim/top/less) aren't supported; see
  ansi_terminal.py.
  """

  _FLUSH_MS        = 30        # coalesce bursts of output into one repaint per tick
  _FLUSH_MAX_CHARS = 200000    # per tick, so a huge dump can't freeze the UI
  _CAPTURE_MAX     = 60000     # raw chars kept for "Analyze with AI"

  def __init__(self, parent, ssh, namespace: str, pod: str, container: str = None, context: str = None, service=None):
    super().__init__(parent)
    self.ssh = ssh if service is None else None
    self._service = service
    self._pod = pod
    self._ns = namespace
    self._container = container
    self.context = context
    self._workers = []
    self._stream_worker = None
    self._pending = []            # output received but not yet painted
    self._raw_since_cmd = ""      # raw output since the last command (for AI analysis)
    self._last_cmd = None
    self._last_exit_code = None   # unknown: this is one long-lived shell, not one process per command
    self._last_stdout = ""
    self._last_stderr = ""
    self._ai_worker = None
    self._ai_dialog = None

    self.setWindowTitle(f"Exec — {pod}")
    self.resize(900, 560)
    apply_qss_to(self)

    layout = QVBoxLayout(self)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)

    info = QLabel(f"Interactive shell in <b>{pod}</b> (namespace: {namespace})")
    info.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
    layout.addWidget(info)

    self.output = QPlainTextEdit()
    self.output.setReadOnly(True)            # typing goes in the box below, not into the history
    self.output.setFont(monospace_font(11))
    self.output.setStyleSheet(
      f"background: #0d0d1a; color: {T['SUCCESS']}; border: none; padding: 8px;"
    )
    self.output.setLineWrapMode(QPlainTextEdit.NoWrap)
    self.output.setUndoRedoEnabled(False)
    layout.addWidget(self.output)
    self._renderer = AnsiStreamRenderer(
      self.output, default_fg=QColor(T['SUCCESS']), default_bg=QColor("#0d0d1a"),
    )
    self.output.viewport().installEventFilter(self)

    self._flush_timer = QTimer(self)
    self._flush_timer.setSingleShot(True)
    self._flush_timer.setInterval(self._FLUSH_MS)
    self._flush_timer.timeout.connect(self._flush_stream)

    self.cmd_inp = QLineEdit()
    self.cmd_inp.setPlaceholderText("Type a command and press Enter…")
    self.cmd_inp.returnPressed.connect(self._send_command)
    layout.addWidget(self.cmd_inp)

    inp_row = QHBoxLayout()
    self.send_btn = QPushButton("Send")
    self.send_btn.setObjectName("primary")
    self.send_btn.clicked.connect(self._send_command)
    inp_row.addWidget(self.send_btn)

    self.interrupt_btn = QPushButton("Ctrl+C")
    self.interrupt_btn.setToolTip("Interrupt the running command (sends Ctrl+C to the pod's shell)")
    self.interrupt_btn.clicked.connect(self._interrupt)
    inp_row.addWidget(self.interrupt_btn)

    self.explain_btn = icon_button(" Analyze with AI")
    self.explain_btn.setToolTip("Ask AI to diagnose the output of the last command")
    self.explain_btn.setEnabled(False)
    self.explain_btn.clicked.connect(self._on_explain)
    inp_row.addWidget(self.explain_btn)
    inp_row.addStretch()
    layout.addLayout(inp_row)

    bb = QDialogButtonBox(QDialogButtonBox.Close)
    bb.rejected.connect(self.reject)
    layout.addWidget(bb)

    QTimer.singleShot(0, self._start_stream)

  # ── terminal geometry ───────────────────────────────────────
  def _terminal_size(self):
    """(cols, rows) that fit the output pane, so remote programs size
    progress bars / wrap lines for the space actually available."""
    fm = QFontMetrics(self.output.font())
    cw = max(1, fm.horizontalAdvance("M"))
    ch = max(1, fm.lineSpacing())
    margin = int(self.output.document().documentMargin())
    vp = self.output.viewport()
    cols = max(20, (vp.width() - 2 * margin) // cw)
    rows = max(5, (vp.height() - 2 * margin) // ch)
    return cols, rows

  def eventFilter(self, obj, event):
    # Watch the *viewport*, not the dialog: it also shrinks when the
    # vertical scrollbar appears (output outgrowing the pane), which the
    # dialog's own resizeEvent never sees — the remote PTY would then be
    # a column too wide and wrap/redraw progress bars wrongly.
    if (event.type() == QEvent.Resize and obj is self.output.viewport()
        and self._stream_worker is not None):
      self._stream_worker.resize(*self._terminal_size())
    return super().eventFilter(obj, event)

  # ── session lifecycle ───────────────────────────────────────
  def _start_stream(self):
    cols, rows = self._terminal_size()
    worker = (
      self._service.create_pod_exec_stream_worker(
        self._ns, self._pod, self._container, self.context, cols=cols, rows=rows,
      )
      if self._service is not None
      else PodExecStreamWorker(
        self.ssh, self._ns, self._pod, self._container, self.context, cols=cols, rows=rows
      )
    )
    worker.chunk.connect(self._append_stream)
    worker.exited.connect(self._on_stream_exit)
    worker.error.connect(self._on_stream_error)
    worker.finished.connect(lambda w=worker: self._on_stream_finished(w))
    self._stream_worker = track_worker(self._workers, worker)
    worker.start()
    self.cmd_inp.setFocus()

  def _shutdown_stream(self):
    """Hang up the remote shell. Called from done(), which every way of
    closing the dialog goes through (window X, the Close button, Esc):
    closeEvent alone is NOT enough — the Close button and Esc call
    reject() directly and never reach it, which would leave the kubectl
    session (and its pooled SSH channel) running on the VM."""
    self._flush_timer.stop()
    worker, self._stream_worker = self._stream_worker, None
    if worker is not None:
      worker.stop()

  def done(self, result):
    self._shutdown_stream()
    super().done(result)

  def _session_over(self, reason: str):
    self.cmd_inp.setEnabled(False)
    self.send_btn.setEnabled(False)
    self.interrupt_btn.setEnabled(False)
    self.cmd_inp.setPlaceholderText(reason)

  def _on_stream_finished(self, worker):
    if self._stream_worker is worker:      # ended on its own (exit, error, dropped connection)
      self._stream_worker = None
      self._session_over("Session ended — close this window and reopen it for a new shell")

  # ── output ──────────────────────────────────────────────────
  def _append_stream(self, text: str):
    if not text:
      return
    self._pending.append(text)
    if self._last_cmd is not None:
      self._raw_since_cmd = (self._raw_since_cmd + text)[-self._CAPTURE_MAX:]
    if not self._flush_timer.isActive():
      self._flush_timer.start()

  def _flush_stream(self, everything: bool = False):
    if not self._pending:
      return
    data = "".join(self._pending)
    self._pending.clear()
    if not everything and len(data) > self._FLUSH_MAX_CHARS:
      self._pending.append(data[self._FLUSH_MAX_CHARS:])
      data = data[:self._FLUSH_MAX_CHARS]
    self._renderer.feed(data)
    if self._pending:
      self._flush_timer.start()            # more to paint: next slice after a short breather
    if self._last_cmd is not None and self._ai_worker is None:
      self.explain_btn.setEnabled(True)

  def _notice(self, text: str, color_code: int):
    """A status line in the terminal itself (exit / error)."""
    self._flush_stream(everything=True)
    self._renderer.feed(f"\r\n\x1b[{color_code}m{text}\x1b[0m\r\n")

  # ── input ───────────────────────────────────────────────────
  def _send_command(self):
    worker = self._stream_worker
    if worker is None:
      return
    line = self.cmd_inp.text()
    if line.strip():
      self._last_cmd = line.strip()
      self._raw_since_cmd = ""
      self._last_exit_code = None
    if worker.send_input(line + "\n"):
      self.cmd_inp.clear()

  def _interrupt(self):
    if self._stream_worker is not None:
      self._stream_worker.interrupt()

  def _on_stream_exit(self, code: int):
    self._notice(f"[session ended — exit code {code}]", 33)

  def _on_stream_error(self, message: str):
    self._notice(f"[error] {message}", 31)

  # ── AI diagnosis of the last command ────────────────────────
  def _on_explain(self):
    if self._ai_worker is not None or self._last_cmd is None:
      return
    provider = ai_assist.get_provider()
    api_key = ai_assist.get_api_key(provider)
    if not api_key:
      label = ai_assist.PROVIDERS.get(provider, {}).get("label", provider)
      QMessageBox.information(
        self, "No API key set",
        f"Add a {label} API key in Settings → AI to use this feature."
      )
      return
    # The pod shell is a single terminal stream, so stdout/stderr aren't
    # separate — hand over what was printed since the last command, with
    # control sequences and progress-bar redraws flattened to plain text.
    self._flush_stream(everything=True)
    self._last_stdout = plain_text(self._raw_since_cmd).strip()[-8000:]
    self._last_stderr = ""
    self._ai_dialog = AIExplainDialog(self, f"AI diagnosis — {self._last_cmd}")
    self._ai_dialog.set_source_context(
      f"Command: {self._last_cmd}\n"
      f"Exit code: unknown (interactive shell)\n\n"
      f"output:\n{self._last_stdout or '(none)'}"
    )
    self._ai_dialog.set_loading()
    self._ai_dialog.show()
    self.explain_btn.setEnabled(False)
    self.explain_btn.setText(" Thinking…")
    worker = ai_assist.AICommandExplainWorker(
      provider, api_key, ai_assist.get_model(provider),
      self._last_cmd, self._last_exit_code, self._last_stderr, self._last_stdout,
    )
    worker.done.connect(self._on_explain_done)
    worker.error.connect(self._on_explain_error)
    worker.finished.connect(self._on_explain_finished)
    self._ai_worker = worker
    worker.start()

  def _on_explain_done(self, text: str):
    if self._ai_dialog is not None:
      self._ai_dialog.set_markdown(text)

  def _on_explain_error(self, message: str):
    if self._ai_dialog is not None:
      self._ai_dialog.set_error(message)

  def _on_explain_finished(self):
    self._ai_worker = None
    self.explain_btn.setEnabled(True)
    self.explain_btn.setText(" Analyze with AI")

  def closeEvent(self, event):
    if self._ai_worker is not None:
      self._ai_worker.quit()
    super().closeEvent(event)

