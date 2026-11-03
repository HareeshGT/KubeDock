from .common import *

class FileTransferDialog(QDialog):
  """OS-style file-transfer sheet with animated progress, speed and ETA."""

  def __init__(self, parent, sftp, direction: str, local_path: str, remote_path: str,
         host: str = None, port: int = 22, user: str = None, pem: str = None,
         password: str = None, sudo_user: str = None, service=None):
    super().__init__(parent)
    self._service = service
    self._session_generation = None
    self._session_snapshot = None
    if service is not None:
      sftp, snapshot = service.capture_transfer_session()
      self._session_generation = snapshot.generation
      self._session_snapshot = snapshot
    self._direction = direction
    self._local   = local_path
    self._remote   = remote_path
    self._speed_buf = []
    self._done    = False

    verb = "Uploading" if direction == "upload" else "Downloading"
    fname = os.path.basename(local_path)
    self.setWindowTitle(f"{verb} — {fname}")
    self.setFixedWidth(480)
    self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
    apply_qss_to(self)

    lay = QVBoxLayout(self)
    lay.setContentsMargins(24, 24, 24, 20)
    lay.setSpacing(14)

    icon_row = QHBoxLayout()
    icon_row.setSpacing(14)
    icon_lbl = QLabel("⬇" if direction == "download" else "⬆")
    icon_lbl.setFont(QFont("Segoe UI Emoji", 28))
    icon_lbl.setStyleSheet(f"color: {T['ACCENT']}; background: transparent;")
    icon_lbl.setFixedWidth(46)
    icon_row.addWidget(icon_lbl)

    text_col = QVBoxLayout()
    text_col.setSpacing(2)
    action_lbl = QLabel(f"{verb}…")
    action_lbl.setFont(QFont("Segoe UI", 13, QFont.Bold))
    action_lbl.setStyleSheet(f"color: {T['TEXT_PRIMARY']}; background: transparent;")
    text_col.addWidget(action_lbl)
    self.file_lbl = QLabel(fname)
    self.file_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px; background: transparent;")
    self.file_lbl.setWordWrap(True)
    text_col.addWidget(self.file_lbl)
    icon_row.addLayout(text_col, 1)
    lay.addLayout(icon_row)

    self.bar = QProgressBar()
    self.bar.setRange(0, 1000)
    self.bar.setValue(0)
    self.bar.setFixedHeight(6)
    self.bar.setTextVisible(False)
    self.bar.setStyleSheet(f"""
      QProgressBar {{ border: none; background: {T['BG_ITEM']}; border-radius: 3px; }}
      QProgressBar::chunk {{
        background: qlineargradient(x1:0,y1:0,x2:1,y2:0,
          stop:0 {T['ACCENT']}, stop:1 {T['ACCENT2']});
        border-radius: 3px;
      }}
    """)
    lay.addWidget(self.bar)

    stats_row = QHBoxLayout()
    stats_row.setSpacing(0)
    self.xfer_lbl = QLabel("0 B / —")
    self.xfer_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px; background: transparent;")
    stats_row.addWidget(self.xfer_lbl)
    stats_row.addStretch()
    self.speed_lbl = QLabel("")
    self.speed_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px; background: transparent;")
    stats_row.addWidget(self.speed_lbl)
    stats_row.addSpacing(16)
    self.eta_lbl  = QLabel("")
    self.eta_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px; background: transparent;")
    stats_row.addWidget(self.eta_lbl)
    lay.addLayout(stats_row)

    dest = remote_path if direction == "upload" else local_path
    dest_lbl = QLabel(f"To: {dest}")
    dest_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 13px; background: transparent;")
    dest_lbl.setWordWrap(True)
    lay.addWidget(dest_lbl)

    btn_row = QHBoxLayout()
    btn_row.addStretch()
    self.cancel_btn = QPushButton("Cancel")
    self.cancel_btn.setObjectName("danger")
    self.cancel_btn.setFixedWidth(90)
    self.cancel_btn.clicked.connect(self._on_cancel)
    btn_row.addWidget(self.cancel_btn)
    lay.addLayout(btn_row)

    self._timer = QTimer(self)
    self._timer.setInterval(500)
    self._timer.timeout.connect(self._update_speed)

    self._t0 = time.monotonic()

    # Use real `scp` on the app's host machine when we can: a plain
    # (non-sudo) connection. Key auth always works. Password auth only
    # works where ScpTransferWorker can answer scp's password prompt
    # over a pty (macOS/Linux) — on Windows there is no pty, so scp
    # would prompt on the local console for the remote password; in
    # that case (ScpTransferWorker.supports() is False) we use the SFTP
    # path below, which reuses the already-authenticated SSH session and
    # never prompts. A sudo-target transfer still needs the SudoFS
    # two-hop dance (upload to a tmp path, then `sudo mv` over ssh),
    # which a single scp invocation can't express, so it also uses SFTP.
    use_scp = (
      (not hasattr(sftp, "_ftp")) and bool(host) and bool(user) and not sudo_user
      and ScpTransferWorker.supports(pem, password)
    )
    total_size = None
    if use_scp:
      try:
        if direction == "upload":
          total_size = os.path.getsize(local_path)
        else:
          total_size = sftp.stat(remote_path).st_size
      except Exception:
        total_size = None

    # scp is only a speed optimisation. The SFTP session is already
    # authenticated, so keep what's needed to fall back to it if scp
    # can't even get going (auth mismatch, host-key change, scp missing,
    # a prompt it can't answer) instead of failing a transfer that the
    # rest of the app proves is perfectly possible.
    self._sftp_args = (sftp, direction, local_path, remote_path)
    self._using_scp = use_scp
    self._progressed = False
    self._retired_workers = []   # keep finished QThreads referenced until they're really gone

    if use_scp:
      worker = ScpTransferWorker(
        host, port, user, pem, password, direction, local_path, remote_path, total_size
      )
    else:
      worker = _TransferWorker(sftp, direction, local_path, remote_path)
    self._start_worker(worker)
    self._timer.start()

  def _session_is_current(self):
    if self._service is None:
      return True
    return self._service.is_current(self._session_generation)

  def _start_worker(self, worker):
    if self._service is not None:
      self._service.bind_worker(worker, self._session_snapshot)
    self._worker = worker
    worker.progress.connect(self._on_progress)
    worker.finished_ok.connect(self._on_success)
    worker.finished_err.connect(self._on_worker_error)
    worker.finished.connect(lambda w=worker: self._on_worker_thread_finished(w))
    worker.start()

  def _on_worker_thread_finished(self, worker):
    """Surface lifecycle cancellation even when a worker emits no result signal."""
    if worker is not self._worker or self._done:
      return
    if self._service is not None and not self._session_is_current():
      self._on_error("The active connection changed; the transfer was cancelled.")
    elif getattr(worker, "_cancelled", False):
      self._on_error("The transfer was cancelled.")

  def _on_worker_error(self, msg: str):
    """scp failing *before any bytes moved* falls back to the SFTP path;
    anything else (SFTP failing, or a failure mid-transfer) is final."""
    if self._service is not None and not self._session_is_current():
      self._on_error(msg)
      return
    if self._using_scp and not self._progressed:
      self._using_scp = False
      first = msg.splitlines()[0] if msg else "unknown error"
      self.file_lbl.setText(f"scp failed ({first[:120]}) — retrying over SFTP…")
      self._retired_workers.append(self._worker)   # its thread is still winding down
      self._t0 = time.monotonic()
      self._speed_buf = []
      sftp, direction, local_path, remote_path = self._sftp_args
      self._start_worker(_TransferWorker(sftp, direction, local_path, remote_path))
      return
    self._on_error(msg)

  def _on_progress(self, done: int, total: int):
    if done > 0:
      self._progressed = True
    elapsed = max(time.monotonic() - self._t0, 0.001)
    speed  = done / elapsed
    if total > 0:
      self.bar.setValue(int(done / total * 1000))
      self.xfer_lbl.setText(f"{size_fmt(done)} / {size_fmt(total)}")
      if speed > 0:
        self.eta_lbl.setText(f"ETA {self._fmt_eta((total - done) / speed)}")
    else:
      self.bar.setRange(0, 0)
      self.xfer_lbl.setText(f"{size_fmt(done)} / —")
    self._speed_buf.append((time.monotonic(), done))
    cutoff = time.monotonic() - 3.0
    self._speed_buf = [(t, b) for t, b in self._speed_buf if t >= cutoff]

  def _update_speed(self):
    if len(self._speed_buf) >= 2:
      t0, b0 = self._speed_buf[0]
      t1, b1 = self._speed_buf[-1]
      dt = t1 - t0
      if dt > 0:
        self.speed_lbl.setText(f"{size_fmt(int((b1 - b0) / dt))}/s")

  def _on_success(self):
    self._done = True
    self._timer.stop()
    self.bar.setRange(0, 1000)
    self.bar.setValue(1000)
    self.bar.setStyleSheet(f"""
      QProgressBar {{ border: none; background: {T['BG_ITEM']}; border-radius: 3px; }}
      QProgressBar::chunk {{ background: {T['SUCCESS']}; border-radius: 3px; }}
    """)
    self.speed_lbl.setText("Done")
    self.eta_lbl.setText("")
    self.cancel_btn.setText("Close")
    self.cancel_btn.setObjectName("success")
    self.cancel_btn.setStyleSheet(
      f"background: transparent; border: 1px solid {T['SUCCESS']}; "
      f"color: {T['SUCCESS']}; border-radius: 7px; padding: 7px 16px;"
    )
    self.cancel_btn.clicked.disconnect()
    self.cancel_btn.clicked.connect(self.accept)
    QTimer.singleShot(1200, self.accept)

  def _on_error(self, msg: str):
    self._done = True
    self._timer.stop()
    self.bar.setRange(0, 1000)
    self.bar.setStyleSheet(f"""
      QProgressBar {{ border: none; background: {T['BG_ITEM']}; border-radius: 3px; }}
      QProgressBar::chunk {{ background: {T['DANGER']}; border-radius: 3px; }}
    """)
    self.speed_lbl.setText("Failed")
    self.eta_lbl.setText("")
    self.file_lbl.setText(f"Error: {msg}")
    self.file_lbl.setStyleSheet(f"color: {T['DANGER']}; font-size: 12px; background: transparent;")
    self.cancel_btn.setText("Close")
    self.cancel_btn.clicked.disconnect()
    self.cancel_btn.clicked.connect(self.reject)

  def _on_cancel(self):
    if not self._done:
      self._done = True
      if self._worker is not None:
        self._worker.cancel()
      self._timer.stop()
    self.reject()

  @staticmethod
  def _fmt_eta(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
      return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
      return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"

  @classmethod
  def download(cls, parent, sftp, remote_path: str, local_path: str,
         host: str = None, port: int = 22, user: str = None, pem: str = None,
         password: str = None, sudo_user: str = None, service=None) -> bool:
    dlg = cls(parent, sftp, "download", local_path, remote_path,
         host=host, port=port, user=user, pem=pem, password=password,
         sudo_user=sudo_user, service=service)
    return dlg.exec_() == QDialog.Accepted

  @classmethod
  def upload(cls, parent, sftp, local_path: str, remote_path: str,
        host: str = None, port: int = 22, user: str = None, pem: str = None,
        password: str = None, sudo_user: str = None, service=None) -> bool:
    dlg = cls(parent, sftp, "upload", local_path, remote_path,
         host=host, port=port, user=user, pem=pem, password=password,
         sudo_user=sudo_user, service=service)
    return dlg.exec_() == QDialog.Accepted

