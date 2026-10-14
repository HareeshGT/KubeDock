"""Dialogs used by the Docker tab: live container logs and a plain text viewer."""

from command_utils import build_prepared_command
from dialogs_parts.common import *
from dialogs_parts.file_exec import _ExecStreamWorker
import docker_utils as du


class DockerTextDialog(QDialog):
  """Read-only monospace viewer for inspect / exec / pull output."""

  def __init__(self, parent, title: str, text: str = ""):
    super().__init__(parent)
    self.setWindowTitle(title)
    self.resize(820, 560)
    apply_qss_to(self)

    layout = QVBoxLayout(self)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)

    self.view = QTextEdit()
    self.view.setReadOnly(True)
    self.view.setFont(monospace_font(11))
    self.view.setStyleSheet(f"background: #0d0d1a; color: {T['TEXT_PRIMARY']}; border: none; padding: 8px;")
    self.view.setPlainText(text)
    layout.addWidget(self.view)

    row = QHBoxLayout()
    row.addStretch()
    copy_btn = QPushButton("Copy")
    copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.view.toPlainText()))
    row.addWidget(copy_btn)
    close_btn = QPushButton("Close")
    close_btn.clicked.connect(self.accept)
    row.addWidget(close_btn)
    layout.addLayout(row)

  def set_text(self, text: str):
    self.view.setPlainText(text)


class DockerLogDialog(QDialog):
  """Live-tails ``docker logs -f`` for one container over SSH."""

  def __init__(self, parent, ssh, container: str, sudo_user=None):
    super().__init__(parent)
    self.ssh = ssh
    self._container = du.validate_name(container, "container")
    self._sudo_user = sudo_user
    self._stream_worker = None
    self._workers = []

    self.setWindowTitle(f"Docker logs — {container}")
    self.resize(900, 600)
    apply_qss_to(self)

    layout = QVBoxLayout(self)
    layout.setContentsMargins(12, 12, 12, 12)
    layout.setSpacing(8)

    ctrl = QHBoxLayout()
    ctrl.addWidget(QLabel("Lines:"))
    self.lines_spin = QSpinBox()
    self.lines_spin.setRange(10, 5000)
    self.lines_spin.setValue(200)
    self.lines_spin.setFixedWidth(80)
    ctrl.addWidget(self.lines_spin)

    self.ts_chk = QCheckBox("Timestamps")
    self.ts_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
    ctrl.addWidget(self.ts_chk)

    self.autoscroll_chk = QCheckBox("Autoscroll")
    self.autoscroll_chk.setChecked(True)
    self.autoscroll_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
    ctrl.addWidget(self.autoscroll_chk)
    ctrl.addStretch()

    self.status_lbl = QLabel("○ connecting…")
    self.status_lbl.setStyleSheet(f"color: {T['TEXT_DIM']};")
    ctrl.addWidget(self.status_lbl)

    restart_btn = QPushButton("↺ Restart")
    restart_btn.setToolTip("Restart the live stream (e.g. after changing Lines / Timestamps)")
    restart_btn.clicked.connect(self._load)
    ctrl.addWidget(restart_btn)

    clear_btn = QPushButton("Clear")
    clear_btn.setToolTip("Clear the screen — the live stream keeps running")
    clear_btn.clicked.connect(lambda: self.log_view.clear())
    ctrl.addWidget(clear_btn)

    copy_btn = QPushButton("Copy")
    copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.log_view.toPlainText()))
    ctrl.addWidget(copy_btn)
    layout.addLayout(ctrl)

    self.log_view = QTextEdit()
    self.log_view.setReadOnly(True)
    self.log_view.setFont(monospace_font(11))
    self.log_view.setStyleSheet(f"background: #0d0d1a; color: {T['SUCCESS']}; border: none; padding: 8px;")
    layout.addWidget(self.log_view)

    bb = QDialogButtonBox(QDialogButtonBox.Close)
    bb.rejected.connect(self.reject)
    layout.addWidget(bb)
    self._load()

  def _load(self):
    self._stop_stream()
    cmd = du.logs_command(
      self._container, tail=self.lines_spin.value(), follow=True,
      timestamps=self.ts_chk.isChecked(),
    )
    cmd = build_prepared_command(cmd, None, self._sudo_user)

    self.log_view.clear()
    self.status_lbl.setText("● live")
    self.status_lbl.setStyleSheet(f"color: {T['SUCCESS']};")

    worker = _ExecStreamWorker(self.ssh, cmd)
    worker.line.connect(self._on_stream_line)
    worker.error.connect(self._on_stream_error)
    worker.finished.connect(self._on_stream_finished)
    self._stream_worker = worker
    track_worker(self._workers, worker)
    worker.start()

  def _stop_stream(self):
    if self._stream_worker is not None:
      self._stream_worker.request_stop()
      self._stream_worker = None

  def _on_stream_line(self, text: str):
    sb = self.log_view.verticalScrollBar()
    at_bottom = sb.value() >= sb.maximum() - 4  # checked BEFORE inserting new text
    cursor = self.log_view.textCursor()
    cursor.movePosition(cursor.End)
    cursor.insertText(text)
    if self.autoscroll_chk.isChecked() and at_bottom:
      sb.setValue(sb.maximum())

  def _on_stream_error(self, err: str):
    self.status_lbl.setText("● error")
    self.status_lbl.setStyleSheet(f"color: {T['DANGER']};")
    self._on_stream_line(f"\n[stream error] {err}\n")

  def _on_stream_finished(self, code: int):
    self._stream_worker = None
    if "error" not in self.status_lbl.text():
      self.status_lbl.setText("○ stream ended")
      self.status_lbl.setStyleSheet(f"color: {T['TEXT_DIM']};")

  def done(self, result):
    # Stop the remote `docker logs -f` when the dialog closes so it does not
    # keep an SSH channel (and a remote process) alive in the background.
    self._stop_stream()
    super().done(result)
