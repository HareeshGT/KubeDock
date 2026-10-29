from .file_exec import _ExecStreamWorker
from .common import *

import json
from PyQt5.QtWidgets import QFileDialog
import ai_intelligence
from .ai_explain import AIExplainDialog

class LogViewerDialog(QDialog):
  """Live-tails pod logs (kubectl logs -f) instead of one-shot fetches.
  Uses _ExecStreamWorker (same streaming machinery as FileExecDialog) to
  keep pushing new lines into the view as they arrive over SSH, rather
  than requiring the user to click Refresh."""

  def __init__(self, parent, ssh, namespace: str, pod: str, container: str = None, context=None):
    super().__init__(parent)
    self.ssh      = ssh
    self._pod      = pod
    self._ns      = namespace
    self._container   = container
    self._context    = context
    self._workers    = []
    self._stream_worker = None  # the currently-running _ExecStreamWorker, if any
    self._ai_worker   = None  # the currently-running AIExplainWorker, if any
    self._evidence_worker = None  # read-only live pod evidence collection
    self._pending_ai_explain = None
    self._ai_dialog   = None
    self._last_resource_evidence = ""

    self.setWindowTitle(f"Logs — {pod}")
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

    self.prev_chk = QCheckBox("Previous container")
    self.prev_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
    ctrl.addWidget(self.prev_chk)

    self.autoscroll_chk = QCheckBox("Autoscroll")
    self.autoscroll_chk.setChecked(True)
    self.autoscroll_chk.setStyleSheet(f"color: {T['TEXT_DIM']};")
    ctrl.addWidget(self.autoscroll_chk)

    ctrl.addStretch()

    self.status_lbl = QLabel("○ connecting…")
    self.status_lbl.setStyleSheet(f"color: {T['TEXT_DIM']};")
    ctrl.addWidget(self.status_lbl)

    restart_btn = QPushButton("↺ Restart")
    restart_btn.setToolTip("Restart the live stream (e.g. after changing Lines / Previous container)")
    restart_btn.clicked.connect(self._load)
    ctrl.addWidget(restart_btn)

    clear_btn = icon_button(" Clear")
    clear_btn.setToolTip("Clear the screen — the live stream keeps running in the background")
    clear_btn.clicked.connect(lambda: self.log_view.clear())
    ctrl.addWidget(clear_btn)

    copy_btn = QPushButton("Copy")
    copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.log_view.toPlainText()))
    ctrl.addWidget(copy_btn)

    self.explain_btn = icon_button(" Analyze with AI")
    self.explain_btn.setToolTip("Ask AI to diagnose this log output")
    self.explain_btn.clicked.connect(lambda: self._on_explain("deep_diagnosis"))
    ctrl.addWidget(self.explain_btn)

    self.local_explain_btn = QPushButton("Analyze Locally")
    self.local_explain_btn.setToolTip("Diagnose using the configured Ollama loopback model; evidence stays on this machine")
    self.local_explain_btn.clicked.connect(lambda: self._on_explain("local_private"))
    ctrl.addWidget(self.local_explain_btn)

    self.export_incident_btn = QPushButton("Export Incident")
    self.export_incident_btn.setToolTip("Save a redacted, timestamped incident evidence bundle as JSON")
    self.export_incident_btn.clicked.connect(self._export_incident_bundle)
    ctrl.addWidget(self.export_incident_btn)
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

  def _build_log_cmd(self, follow: bool) -> str:
    n  = self.lines_spin.value()
    prev = "--previous" if self.prev_chk.isChecked() else ""
    c  = f"-c {self._container}" if self._container else ""
    f  = "-f" if follow else ""
    inner = f"kubectl logs --tail={n} -n {self._ns} {prev} {c} {f} {self._pod} 2>&1"
    # exec_command() opens a non-login shell, which skips /etc/profile —
    # exactly where kubectl's PATH entry usually lives (Homebrew, snap,
    # etc.). "bash -lc" forces a login shell so those get sourced.
    return f"bash -lc {shlex.quote(inner)}"

  def _load(self):
    # Kill whatever stream is already running before starting a fresh one
    # (e.g. user changed Lines / Previous container and hit Restart).
    if self._stream_worker is not None:
      self._stream_worker.request_stop()
      self._stream_worker = None

    # -f and --previous don't mix meaningfully (a terminated container's
    # logs can't be followed), so fall back to a one-shot fetch for that case.
    follow = not self.prev_chk.isChecked()
    cmd = self._build_log_cmd(follow=follow)

    if not follow:
      self.log_view.setPlainText("Loading…")
      self.status_lbl.setText("○ static (previous container)")
      self.status_lbl.setStyleSheet(f"color: {T['TEXT_DIM']};")
      worker = CommandWorker(self.ssh, cmd)
      worker.done.connect(self.log_view.setPlainText)
      worker.error.connect(lambda e: self.log_view.setPlainText(f"[error] {e}"))
      track_worker(self._workers, worker)
      worker.start()
      return

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
    # track_worker() already removes the worker from self._workers once
    # its finished signal fires; only the dialog-local reference needs
    # clearing here.
    self._stream_worker = None
    if "error" not in self.status_lbl.text():
      self.status_lbl.setText("○ stream ended")
      self.status_lbl.setStyleSheet(f"color: {T['TEXT_DIM']};")


  def _export_incident_bundle(self):
    """Export a redacted evidence snapshot without sending it to an AI provider."""
    log_text = self.log_view.toPlainText().strip()
    if not log_text or log_text == "Loading…":
      QMessageBox.information(self, "No incident evidence", "Wait for log output before exporting an incident bundle.")
      return
    bundle = ai_intelligence.build_incident_bundle(
      title=f"Pod incident: {self._pod}",
      cluster=str(self._context or "current context"),
      namespace=str(self._ns or ""),
      resource=f"pod/{self._pod}",
      evidence={
        "container": self._container or "default",
        "previous_container_logs": bool(self.prev_chk.isChecked()),
        "logs": log_text,
        "live_resource_evidence": self._last_resource_evidence or "not collected",
        "candidate_signals": ai_intelligence.correlate_incident_signals(
          logs=log_text.splitlines(),
          events=(self._last_resource_evidence or "").splitlines(),
        ),
      },
    )
    path, _ = QFileDialog.getSaveFileName(
      self, "Export Incident Bundle", f"{self._pod}-incident.json",
      "JSON files (*.json)"
    )
    if not path:
      return
    try:
      with open(path, "w", encoding="utf-8") as handle:
        json.dump(bundle, handle, indent=2, ensure_ascii=False)
        handle.write(chr(10))
    except OSError as exc:
      QMessageBox.warning(self, "Incident Export Failed", str(exc))
      return
    QMessageBox.information(self, "Incident Exported", f"Redacted incident evidence saved to: {path}")

  # ── AI diagnosis ──────────────────────────────────────────
  def _on_explain(self, task_profile="deep_diagnosis"):
    if self._ai_worker is not None or self._evidence_worker is not None:
      return

    log_text = self.log_view.toPlainText().strip()
    if not log_text or log_text == "Loading…":
      QMessageBox.information(self, "Nothing to explain", "There's no log output yet.")
      return

    ai_settings = ai_assist.get_ai_settings()
    assignment = ai_settings.get("task_profiles", {}).get(task_profile) or {}
    provider = assignment.get("provider") or ai_settings["provider"]
    api_key = ai_assist.get_api_key(provider)
    if not api_key and provider != "ollama":
      label = ai_assist.PROVIDERS.get(provider, {}).get("label", provider)
      QMessageBox.information(
        self, "No API key set",
        f"Add a {label} API key in Settings → AI to use this profile."
      )
      return
    if provider == "ollama" and task_profile != "local_private":
      # Ollama remains an explicit local choice, never a silent fallback.
      QMessageBox.information(
        self, "Local model selected",
        "Ollama is configured for this task. Ensure Ollama is running at 127.0.0.1:11434."
      )

    self._pending_ai_explain = {
      "provider": provider,
      "api_key": api_key,
      "model": ai_assist.get_model(provider),
      "task_profile": task_profile,
      "log_text": log_text,
    }
    self._ai_dialog = AIExplainDialog(self, f"AI diagnosis — {self._pod}")
    self._ai_dialog.set_source_context(
      f"Pod: {self._pod}\nNamespace: {self._ns or '(unknown)'}\n"
      f"Container: {self._container or 'default'}\n\nContainer logs:\n{log_text}"
    )
    self._ai_dialog.set_loading()
    self._ai_dialog.show()

    self.explain_btn.setEnabled(False)
    self.explain_btn.setText(" Collecting…")
    self.status_lbl.setText("○ collecting live pod status, events and metrics…")

    if not self._ns:
      self._start_ai_explain(
        "Live Kubernetes evidence was not collected because the pod namespace is unknown."
      )
      return

    # One bounded, read-only SSH worker gathers describe output (conditions,
    # image/config references, state and recent events) and metrics if
    # metrics-server is available. It never retrieves Kubernetes Secret data.
    ns_arg = shlex.quote(self._ns)
    pod_arg = shlex.quote(self._pod)
    container_flag = (
      f"-c {shlex.quote(str(self._container))} "
      if self._container else ""
    )
    # The displayed stream already has either current or previous logs. Fetch
    # the alternate stream once, bounded to 100 lines, rather than rereading
    # the same stream unnecessarily.
    alternate_previous_flag = "" if self.prev_chk.isChecked() else "--previous "
    alternate_stream = "current" if self.prev_chk.isChecked() else "previous"
    inner = (
      f"printf '%s\\n' '--- kubectl describe pod ---'; "
      f"kubectl -n {ns_arg} describe pod {pod_arg} 2>&1; "
      f"printf '%s\\n' '--- {alternate_stream} container logs (tail 100) ---'; "
      f"kubectl logs --tail=100 -n {ns_arg} {alternate_previous_flag}"
      f"{container_flag}{pod_arg} 2>&1; "
      "printf '%s\\n' '--- kubectl top pod --containers ---'; "
      f"kubectl -n {ns_arg} top pod {pod_arg} --containers 2>&1"
    )
    worker = CommandWorker(self.ssh, f"bash -lc {shlex.quote(inner)}", timeout=25)
    worker.result.connect(self._on_resource_evidence)
    worker.error.connect(self._on_resource_evidence_error)
    self._evidence_worker = worker
    track_worker(self._workers, worker)
    worker.start()

  def _on_resource_evidence(self, output: str, err: str, exit_code: int):
    self._evidence_worker = None
    parts = [(output or "").strip(), (err or "").strip()]
    evidence = "\n".join(part for part in parts if part)
    if not evidence:
      evidence = (
        f"Live pod evidence collection returned no output (exit code {exit_code}). "
        "Treat status, events and metrics as unknown."
      )
    elif exit_code != 0:
      evidence += (
        f"\n\nEvidence command exit code: {exit_code}. Some sections may be unavailable; "
        "do not infer missing status or metrics."
      )
    self._last_resource_evidence = ai_intelligence.redact_evidence(evidence)
    self._start_ai_explain(evidence)

  def _on_resource_evidence_error(self, error: str):
    self._evidence_worker = None
    safe_error = ai_assist._redact_sensitive_text(str(error or "unknown error"))
    self._last_resource_evidence = "collection failed: " + safe_error
    self._start_ai_explain(
      "Live Kubernetes status/events/metrics collection failed: " + safe_error
    )

  def _start_ai_explain(self, resource_context: str):
    pending = self._pending_ai_explain
    if not pending:
      self.explain_btn.setEnabled(True)
      self.explain_btn.setText(" Analyze with AI")
      return

    log_text = pending["log_text"]
    full_context = (
      f"Pod: {self._pod}\nNamespace: {self._ns or '(unknown)'}\n"
      f"Container: {self._container or 'default'}\n\n"
      "Live Kubernetes resource evidence (describe/events/configuration/status and "
      f"available metrics):\n{resource_context}\n\nContainer logs:\n{log_text}"
    )
    if self._ai_dialog is not None:
      self._ai_dialog.set_source_context(full_context)

    self.status_lbl.setText("● analyzing logs and live resource evidence…")
    worker = ai_assist.AIExplainWorker(
      pending["provider"], pending["api_key"], pending["model"],
      self._pod, self._ns, self._container, log_text,
      resource_context=resource_context,
      task_profile=pending.get("task_profile", "deep_diagnosis"),
    )
    worker.done.connect(self._on_explain_done)
    worker.error.connect(self._on_explain_error)
    self._ai_worker = worker
    worker.finished.connect(self._on_explain_finished)
    worker.start()

  def _on_explain_done(self, text: str):
    if self._ai_dialog is not None:
      self._ai_dialog.set_markdown(text)

  def _on_explain_error(self, message: str):
    if self._ai_dialog is not None:
      self._ai_dialog.set_error(message)

  def _on_explain_finished(self):
    self._ai_worker = None
    self._pending_ai_explain = None
    self.explain_btn.setEnabled(True)
    self.explain_btn.setText(" Analyze with AI")

  def closeEvent(self, event):
    # Stop the SSH channel/thread rather than leaking it once the dialog closes.
    if self._stream_worker is not None:
      self._stream_worker.request_stop()
    if self._ai_worker is not None:
      self._ai_worker.quit()
    super().closeEvent(event)

