from .common import *

def _exec_cmd_for(ext: str, quoted: str, dirpath: str) -> tuple:
  q = quoted
  d = f"'{dirpath}'"
  _map = {
    ".py":  ("Python 3",  f"cd {d} && python3 -u {q}"),
    ".sh":  ("Bash",    f"cd {d} && bash {q}"),
    ".bash": ("Bash",    f"cd {d} && bash {q}"),
    ".rb":  ("Ruby",    f"cd {d} && ruby {q}"),
    ".js":  ("Node.js",   f"cd {d} && node {q}"),
    ".ts":  ("ts-node",   f"cd {d} && ts-node {q}"),
    ".php":  ("PHP",     f"cd {d} && php {q}"),
    ".pl":  ("Perl",    f"cd {d} && perl {q}"),
    ".lua":  ("Lua",     f"cd {d} && lua {q}"),
    ".r":   ("Rscript",   f"cd {d} && Rscript {q}"),
    ".go":  ("Go run",   f"cd {d} && go run {q}"),
    ".java": ("javac+java", f"cd {d} && javac {q} && java $(basename {q} .java)"),
    ".kt":  ("kotlinc",   f"cd {d} && kotlinc {q} -include-runtime -d /tmp/_kt_out.jar && java -jar /tmp/_kt_out.jar"),
    ".rs":  ("rustc",    f"cd {d} && rustc {q} -o /tmp/_rs_out && /tmp/_rs_out"),
    ".c":   ("gcc",     f"cd {d} && gcc {q} -o /tmp/_c_out && /tmp/_c_out"),
    ".cpp":  ("g++",     f"cd {d} && g++ {q} -o /tmp/_cpp_out && /tmp/_cpp_out"),
    ".swift": ("swift",    f"cd {d} && swift {q}"),
  }
  return _map.get(ext, (None, None))


class _ExecStreamWorker(QThread):
  """Streams SSH command output line by line via signals."""
  line   = pyqtSignal(str)
  error  = pyqtSignal(str)
  finished = pyqtSignal(int)  # exit code

  def __init__(self, ssh, cmd: str):
    super().__init__()
    self._ssh   = ssh
    self._cmd   = cmd
    self._stop  = False
    self._channel = None  # set once the session is opened; used by send_input()
    self.finished.connect(self.deleteLater)

  def request_stop(self):
    self._stop = True

  def send_input(self, text: str):
    """Write text to the running remote process's stdin (via the PTY).
    Safe to call from the UI thread — Paramiko channels serialize
    sends internally. No-ops silently if there's no live channel yet
    or the command has already finished."""
    if self._channel is None:
      return
    try:
      self._channel.send(text)
    except Exception:
      pass

  def run(self):
    channel = None
    exit_code = -1
    try:
      channel = open_managed_session(self._ssh)
      channel.get_pty()
      channel.settimeout(0.5)
      channel.exec_command(self._cmd)
      self._channel = channel

      while True:
        if self._stop:
          exit_code = -1
          return
        try:
          if channel.recv_ready():
            chunk = channel.recv(4096)
            if chunk:
              self.line.emit(chunk.decode("utf-8", errors="replace"))
            continue
        except Exception:
          pass
        if channel.exit_status_ready() and not channel.recv_ready():
          break
        self.msleep(50)

      exit_code = channel.recv_exit_status() if not self._stop else -1
    except Exception as e:
      self.error.emit(str(e))
      exit_code = -1
    finally:
      self._channel = None
      close_managed_session(channel)
      # Always notify the dialog that this worker is finished, including
      # when Stop caused run() to return early. This keeps the worker
      # lifecycle consistent and lets the dialog drop its reference before
      # Qt deletes the QThread wrapper.
      self.finished.emit(exit_code)


class FileExecDialog(QDialog):
  """Run a remote script and stream its output line by line."""

  def __init__(self, parent, ssh, remote_path: str, sudo_user=None):
    super().__init__(parent)
    self._ssh    = ssh
    self._remote  = remote_path
    self._sudo_user = sudo_user
    self._worker  = None

    fname = os.path.basename(remote_path)
    ext  = os.path.splitext(fname)[1].lower()
    self.setWindowTitle(f"Run — {fname}")
    self.resize(880, 580)
    apply_qss_to(self)

    lay = QVBoxLayout(self)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    # ── Toolbar ───────────────────────────────────────────
    tb_widget = QWidget()
    tb_widget.setFixedHeight(48)
    tb_widget.setStyleSheet(
      f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
    )
    tb = QHBoxLayout(tb_widget)
    tb.setContentsMargins(12, 0, 12, 0)
    tb.setSpacing(10)

    tb.addWidget(QLabel("Interpreter:"))
    self._interp_combo = QComboBox()
    self._interp_combo.setMinimumWidth(130)
    tb.addWidget(self._interp_combo)

    tb.addWidget(QLabel("Args:"))
    self._args_inp = QLineEdit()
    self._args_inp.setPlaceholderText("optional arguments…")
    self._args_inp.setMinimumWidth(220)
    self._args_inp.returnPressed.connect(self._run)
    tb.addWidget(self._args_inp, 1)

    self._run_btn = QPushButton("▶ Run")
    self._run_btn.setObjectName("primary")
    self._run_btn.setFixedWidth(90)
    self._run_btn.clicked.connect(self._run)
    tb.addWidget(self._run_btn)

    self._stop_btn = QPushButton("■ Stop")
    self._stop_btn.setObjectName("danger")
    self._stop_btn.setFixedWidth(90)
    self._stop_btn.setEnabled(False)
    self._stop_btn.clicked.connect(self._stop)
    tb.addWidget(self._stop_btn)

    # Declare _output before clr_btn so the lambda works
    self._output = QTextEdit()
    self._output.setReadOnly(True)
    self._output.setFont(monospace_font(11))
    self._output.setStyleSheet(
      f"background: #0d0d1a; color: {T['SUCCESS']}; border: none; padding: 10px;"
    )

    clr_btn = QPushButton("Clear")
    clr_btn.setFixedWidth(60)
    clr_btn.clicked.connect(self._output.clear)
    tb.addWidget(clr_btn)
    lay.addWidget(tb_widget)

    lay.addWidget(self._output)

    # ── Runtime input bar ──────────────────────────────────
    # For scripts that block on input() / read / a sudo password prompt
    # etc. The remote process runs under a PTY (see _ExecStreamWorker),
    # so whatever is typed here is written straight to its stdin — the
    # script keeps running instead of hanging forever waiting on a
    # terminal that was never actually connected to anything.
    in_widget = QWidget()
    in_widget.setFixedHeight(44)
    in_widget.setStyleSheet(
      f"background: {T['BG_PANEL']}; border-top: 1px solid {T['BORDER']};"
    )
    in_lay = QHBoxLayout(in_widget)
    in_lay.setContentsMargins(12, 6, 12, 6)
    in_lay.setSpacing(8)

    in_lay.addWidget(QLabel("Input:"))
    self._input_inp = QLineEdit()
    self._input_inp.setPlaceholderText(
      "Type input for the running script and press Enter…"
    )
    self._input_inp.setEnabled(False)
    self._input_inp.returnPressed.connect(self._send_input)
    in_lay.addWidget(self._input_inp, 1)

    self._input_mask_btn = icon_button("")
    self._input_mask_btn.setCheckable(True)
    self._input_mask_btn.setFixedWidth(32)
    self._input_mask_btn.setToolTip("Mask input (for passwords/secrets)")
    self._input_mask_btn.toggled.connect(self._toggle_input_echo)
    in_lay.addWidget(self._input_mask_btn)

    self._send_btn = QPushButton("Send")
    self._send_btn.setFixedWidth(70)
    self._send_btn.setEnabled(False)
    self._send_btn.clicked.connect(self._send_input)
    in_lay.addWidget(self._send_btn)

    lay.addWidget(in_widget)

    # ── Status bar ────────────────────────────────────────
    sb_widget = QWidget()
    sb_widget.setFixedHeight(26)
    sb_widget.setStyleSheet(
      f"background: {T['BG_PANEL']}; border-top: 1px solid {T['BORDER']};"
    )
    sb = QHBoxLayout(sb_widget)
    sb.setContentsMargins(12, 0, 12, 0)
    sb.setSpacing(0)

    self._status_lbl = QLabel("Ready")
    self._status_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 13px;")
    sb.addWidget(self._status_lbl)
    sb.addStretch()

    copy_btn = QPushButton("Copy output")
    copy_btn.setFixedWidth(100)
    copy_btn.clicked.connect(
      lambda: QApplication.clipboard().setText(self._output.toPlainText())
    )
    sb.addWidget(copy_btn)
    lay.addWidget(sb_widget)

    self._populate_interpreters(ext)

  # ── Interpreter combo ─────────────────────────────────────
  def _populate_interpreters(self, ext: str):
    label, _ = _exec_cmd_for(ext, "", "")
    options = []
    if label:
      options.append(label)
    if ext in (".sh", ".bash") and "Bash" not in options:
      options.append("Bash")
    if ext == ".py" and "Python 3" not in options:
      options.append("Python 3")
    options += ["sh (raw)", "Custom…"]
    self._interp_combo.addItems(options)
    self._interp_combo.currentTextChanged.connect(self._on_interp_change)

  def _on_interp_change(self, text: str):
    if text == "Custom…":
      self._args_inp.setPlaceholderText("interpreter command (e.g. python3.11 -u)")
    else:
      self._args_inp.setPlaceholderText("optional arguments…")

  # ── Build shell command ───────────────────────────────────
  def _quoted(self, p: str) -> str:
    return "'" + p.replace("'", "'\\''") + "'"

  def _build_command(self) -> str:
    ext   = os.path.splitext(self._remote)[1].lower()
    dirpath = os.path.dirname(self._remote) or "/"
    qpath  = self._quoted(self._remote)
    qdir  = self._quoted(dirpath)
    args  = self._args_inp.text().strip()
    choice = self._interp_combo.currentText()

    if choice == "Custom…":
      interp = args or "bash"
      return f"cd {qdir} && {interp} {qpath}"
    if choice == "sh (raw)":
      return (f"cd {qdir} && chmod +x {qpath} && {qpath} {args}").rstrip()

    _, cmd = _exec_cmd_for(ext, qpath, dirpath)
    if cmd:
      return (f"{cmd} {args}").rstrip() if args else cmd

    return (f"cd {qdir} && {choice} {qpath} {args}").rstrip()

  # ── Run / stop ────────────────────────────────────────────
  def _worker_is_running(self) -> bool:
    """Return whether the current worker is alive without touching a stale
    PyQt wrapper after Qt has already deleted its underlying QObject."""
    worker = self._worker
    if worker is None:
      return False
    try:
      return worker.isRunning()
    except RuntimeError:
      # Qt has deleted the C++ QThread while Python still holds the wrapper.
      self._worker = None
      return False

  def _run(self):
    if self._worker_is_running():
      return

    cmd = self._build_command()
    if self._sudo_user:
      safe = cmd.replace("'", "'\\''")
      cmd = f"sudo -u {self._sudo_user} sh -c '{safe}'"

    append_terminal_html(self._output, f"<span style='color:{T['ACCENT2']}'>$ {html_escape(cmd)}</span>")
    self._set_status("Running…", T['WARNING'])
    self._run_btn.setEnabled(False)
    self._stop_btn.setEnabled(True)
    self._input_inp.setEnabled(True)
    self._send_btn.setEnabled(True)
    self._input_inp.setFocus()

    self._worker = _ExecStreamWorker(self._ssh, cmd)
    self._worker.line.connect(self._on_line)
    self._worker.error.connect(self._on_error)
    self._worker.finished.connect(self._on_finished)
    self._worker.start()

  def _stop(self):
    worker = self._worker
    if worker is not None:
      try:
        if worker.isRunning():
          worker.request_stop()
      except RuntimeError:
        self._worker = None
    append_terminal_html(self._output, f"<span style='color:{T['WARNING']}'>[stopped by user]</span>")
    self._run_btn.setEnabled(True)
    self._stop_btn.setEnabled(False)
    self._input_inp.setEnabled(False)
    self._send_btn.setEnabled(False)
    self._set_status("Stopped", T['WARNING'])

  def _send_input(self):
    """Send whatever's in the input box to the running process's stdin,
    followed by Enter — same as typing it at a real terminal prompt.

    The remote PTY normally echoes typed input back into the output
    stream itself (that's how a real terminal works), so for plain
    input we don't print it a second time here — it'll show up via
    _on_line once the remote's line discipline reflects it back.
    For masked entries (passwords), the remote side almost always
    disables echo before reading, so nothing would appear on screen
    at all — we print a masked marker ourselves so there's still
    visible confirmation that something was sent."""
    if not self._worker_is_running():
      return
    text = self._input_inp.text()
    if self._input_mask_btn.isChecked():
      append_terminal_html(
        self._output,
        f"<span style='color:{T['INFO']}'>&gt; {'*' * len(text)}</span>",
      )
    self._worker.send_input(text + "\n")
    self._input_inp.clear()

  def _toggle_input_echo(self, checked: bool):
    self._input_inp.setEchoMode(QLineEdit.Password if checked else QLineEdit.Normal)

  def _on_line(self, line: str):
    append_terminal_text(self._output, line + "\n")
    sb = self._output.verticalScrollBar()
    sb.setValue(sb.maximum())

  def _on_error(self, err: str):
    append_terminal_html(self._output, f"<span style='color:{T['DANGER']}'>[error] {html_escape(err)}</span>")

  def _on_finished(self, code: int):
    self._run_btn.setEnabled(True)
    self._stop_btn.setEnabled(False)
    self._input_inp.setEnabled(False)
    self._send_btn.setEnabled(False)
    color = T['SUCCESS'] if code == 0 else T['DANGER']
    append_terminal_html(self._output, f"<span style='color:{color}'>[exit code {code}]</span>")
    # Drop the Python reference as soon as execution finishes. The worker
    # is connected to deleteLater(), so retaining this reference until the
    # next Run could leave a Python wrapper around an already-deleted C++
    # QThread and make isRunning() raise RuntimeError.
    self._worker = None
    self._set_status(f"Finished (exit {code})", color)

  def _set_status(self, msg, color=None):
    self._status_lbl.setText(msg)
    self._status_lbl.setStyleSheet(
      f"color: {color or T['TEXT_MUTED']}; font-size: 13px;"
    )

