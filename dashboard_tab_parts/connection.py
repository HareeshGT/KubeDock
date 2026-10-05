from dashboard_tab import *
from dashboard_tab import REFRESH_MS, BACKGROUND_REFRESH_MS, _FTPDashboardWorker, QEvent, size_fmt

"""DashboardTab implementation mixin.

The public DashboardTab class remains in dashboard_tab.py; this module only
contains logically grouped DashboardTab methods.
"""

class DashboardConnectionMixin:
  def set_kube_context(self, context):
      """Follow the Kubernetes tab's selected context without changing
      the jump host's global kubectl current-context."""
      context = str(context or "").strip()
      if context == self._kube_context:
        return
      self._kube_context = context
      if self._active and self.ssh:
        self._refresh()
  
  
  def set_connection(self, protocol, fs=None, ssh=None, host="", port=None, user=""):
      """Bind the dashboard to either SSH or FTP/FTPS."""
      self._connection_protocol = (protocol or "").lower()
      self._connection_host = host or getattr(fs, "host", "") or ""
      self._connection_port = port or getattr(fs, "port", None)
      self._connection_user = user or getattr(fs, "user", "") or ""
      self._ftp_current_path = "/"
      self.ftp = fs if self._connection_protocol in ("ftp", "ftps") else None
      self.set_ssh(ssh) if self._connection_protocol == "ssh" else self._set_ftp_connection(fs)
  
  
  def _set_ftp_connection(self, fs):
      self._snapshot_generation += 1
      self._busy = False
      self._cycle_background = False
      self._bg_cache = None
      self._bg_timer.stop()
      self.ssh = None
      self.ftp = fs
      self._history_key = None
      self._history = []
      self._last_history_time = 0
      self._render_history()
      if fs:
        self._show_ftp_placeholder()
        if self._active:
          self._refresh_ftp()
          self._timer.start(REFRESH_MS)
      else:
        self._timer.stop()
        self._process_timer.stop()
        self._process_generation += 1
        self._process_busy = False
        self._show_disconnected()
  
  
  def set_ftp_path(self, path):
      self._ftp_current_path = str(path or "/")
      if self._active and self.ftp:
        self._refresh_ftp()
  
  
  def _show_ftp_placeholder(self):
      self.disconnected_lbl.hide()
      self.vm_card["frame"].hide()
      self.k8s_summary_card["frame"].hide()
      self.workloads_card["frame"].hide()
      self.services_card["frame"].hide()
      self.events_card["frame"].hide()
      self.history_card["frame"].hide()
      self.k8s_card["frame"].hide()
      self.ftp_card["frame"].show()
      self._update_live_label()
  
  
  def _render_ftp_snapshot(self, snap):
      self._ftp_snapshot = snap
      vals = {
        "protocol": snap.get("protocol", "—"),
        "host": snap.get("host", "—"),
        "port": str(snap.get("port", "—")),
        "user": snap.get("user", "—"),
        "security": "TLS / encrypted" if snap.get("tls") else "Plain FTP",
        "mode": "Passive" if snap.get("passive") else "Active",
        "cwd": snap.get("cwd", "—"),
        "system": snap.get("system", "—"),
        "items": str(snap.get("items", 0)),
        "files": str(snap.get("files", 0)),
        "folders": str(snap.get("folders", 0)),
        "total": size_fmt(snap.get("total_bytes", 0)),
        "latency": f'{snap.get("latency_ms"):.0f} ms' if snap.get("latency_ms") is not None else "Unavailable",
        "uptime": snap.get("uptime", "—"),
        "encoding": snap.get("encoding", "—"),
        "timeout": f'{snap.get("timeout")} s' if snap.get("timeout") else "—",
        "tls_version": snap.get("tls_version", "—"),
        "tls_cipher": snap.get("tls_cipher", "—"),
        "largest": f'{snap.get("largest_name", "—")} ({size_fmt(snap.get("largest_size", 0))})',
        "newest": f'{snap.get("newest_name", "—")} ({snap.get("newest_age", "—")} ago)' if snap.get("newest_name") not in (None, "—") else "Unavailable",
        "downloaded": size_fmt(snap.get("downloaded_bytes", 0)),
        "uploaded": size_fmt(snap.get("uploaded_bytes", 0)),
        "download_count": str(snap.get("download_count", 0)),
        "upload_count": str(snap.get("upload_count", 0)),
        "last_operation": f'{snap.get("last_operation", "None")} ({snap.get("last_operation_age", "—")} ago)',
      }
      for key, value in vals.items():
        field = self._ftp_fields.get(key)
        if field is not None:
          field.setText(value)
  
      feature_text = snap.get("feature_text") or "Not reported"
      ext = snap.get("extensions") or []
      ext_text = "  ·  ".join(f".{k}: {v}" for k, v in ext) if ext else "No file-extension data"
      self.ftp_features.setText(
        f'Server banner: {snap.get("welcome") or "Not reported"}\n'
        f'Features: {feature_text}\n'
        f'File types: {ext_text}'
      )
      self.updated_lbl.setText(f"Updated {time.strftime('%H:%M:%S')}")
  
  
  def _refresh_ftp(self):
      if not self.ftp or not self._active or self._ftp_busy:
        return
      self._ftp_busy = True
      worker = _FTPDashboardWorker(self.ftp, getattr(self, "_ftp_current_path", "/"))
      self._ftp_worker = worker
      worker.done.connect(self._on_ftp_snapshot)
      worker.error.connect(self._on_ftp_error)
      worker.finished.connect(self._ftp_worker_done)
      worker.start()
  
  
  def _on_ftp_snapshot(self, snap):
      if self.ftp is None:
        return
      self._render_ftp_snapshot(snap)
      self.status_msg.emit("FTP dashboard updated")
  
  
  def _on_ftp_error(self, message):
      if self.ftp:
        self.status_msg.emit(f"FTP dashboard: {message}")
  
  
  def _ftp_worker_done(self):
      self._ftp_busy = False
      self._ftp_worker = None
  
  
  def set_ssh(self, ssh):
      self._connection_protocol = "ssh" if ssh else None
      self.ftp = None
      # Invalidate callbacks from any in-flight collection belonging to the
      # previous SSH connection.
      self._snapshot_generation += 1
      self._busy = False
      self._cycle_background = False
      self._bg_cache = None
      self.ssh = ssh
      self._history_key = self._derive_history_key(ssh)
      self._history = self._load_history()
      self._last_history_time = 0
      self._render_history()
      if ssh:
        self._show_connected_placeholder()
        if self._active:
          self._refresh()
          self._timer.start(REFRESH_MS)
          self._process_timer.start()
        else:
          self._bg_timer.start(BACKGROUND_REFRESH_MS)
      else:
        self._timer.stop()
        self._bg_timer.stop()
        self._process_timer.stop()
        self._process_generation += 1
        self._process_busy = False
        self._busy = False
        self._host_snapshot = None
        # The live-process UI is rendered as structured metrics + a process tree.
        # The old raw-top QText widget no longer exists.
        process_tree = getattr(self, "process_tree", None)
        if process_tree is not None:
          process_tree.clear()
        for widget in getattr(self, "process_metrics", {}).values():
          widget.setText("—")
        detail = getattr(self, "process_detail_lbl", None)
        if detail is not None:
          detail.setText("CPU — · Memory — · Load —")
        self.process_status.setText("Disconnected")
        self._k8s_snapshot = None
        self._show_disconnected()
        for win in list(self._node_windows.values()):
          win.close()
  
  
  @staticmethod
  def _derive_history_key(ssh):
      """Identify 'which instance' for history separation as ip:port off
      the live SSH transport, so dev/test/prod never share one history
      line just because they were connected to in the same session."""
      if ssh is None:
        return None
      try:
        peer = ssh.get_transport().getpeername()
        return f"{peer[0]}:{peer[1]}"
      except Exception:
        return None
  
  
  def set_active(self, active: bool):
      """Called by main_window whenever this tab becomes the visible
      dashboard (active=True) or is navigated away from (active=False).

      Focused: the normal REFRESH_MS timer drives collection (unchanged).
      Unfocused: that timer is stopped and _bg_timer runs the same
      collection at most once per BACKGROUND_REFRESH_MS."""
      active = bool(active)
      if not active and not self._active:
        # Already unfocused (main_window re-reports this on every tab
        # change). Don't restart the background cadence or invalidate an
        # in-flight background cycle.
        return
      self._active = active
      if active and self.ssh:
        self._bg_timer.stop()
        self._resume_focused()
        self._timer.start(REFRESH_MS)
        self._process_timer.start()
      elif active and self.ftp:
        self._refresh_ftp()
        self._timer.start(REFRESH_MS)
      else:
        self._timer.stop()
        self._process_timer.stop()
        self._process_generation += 1
        self._process_busy = False
        # Invalidate a cycle when leaving the tab so late worker signals
        # cannot publish data after the dashboard has been paused.
        self._snapshot_generation += 1
        self._busy = False
        self._cycle_background = False
        if self.ssh:
          self._bg_timer.start(BACKGROUND_REFRESH_MS)
        else:
          self._bg_timer.stop()


  def _resume_focused(self):
      """Back on the Dashboard: show the newest background result at once,
      then resume the normal refresh without duplicating a fresh/in-flight one."""
      cache, self._bg_cache = self._bg_cache, None
      fresh = False
      if cache and cache["kube_context"] == self._kube_context:
        self._publish_snapshot(cache["host"], cache["k8s"], cache["k8s_error"], cache["elapsed"])
        fresh = (time.monotonic() - cache["at"]) * 1000 < REFRESH_MS
      if self._busy and self._cycle_background:
        # A background cycle is already running: adopt it instead of
        # starting a second one; it will render when it completes.
        self._cycle_background = False
        self._update_live_label()
        return
      self._update_live_label()
      if not fresh:
        self._refresh()


  def _background_refresh(self):
      """Unfocused tick (<= once per BACKGROUND_REFRESH_MS)."""
      if self._active or not self.ssh:
        return
      self._refresh(background=True)


  def shutdown(self):
      """Stop every timer and invalidate in-flight work (app closing)."""
      self._active = False
      self._timer.stop()
      self._bg_timer.stop()
      self._process_timer.stop()
      self._process_generation += 1
      self._process_busy = False
      self._snapshot_generation += 1
      self._busy = False
      self._cycle_background = False
      self._bg_cache = None
  
  
  def changeEvent(self, event):
      # Theme changes update the QApplication palette. Re-apply all of the
      # Dashboard's inline styles so labels created under the previous theme
      # cannot retain e.g. Rose Gold text after switching to Snow Light.
      if event.type() == QEvent.ApplicationPaletteChange:
        self.apply_theme()
      super().changeEvent(event)
  
  
  def apply_theme(self):
      self._apply_styles()
      self.cpu_ring["ring"].refresh_theme()
      self.mem_ring["ring"].refresh_theme()
      self.storage_ring["ring"].refresh_theme()
      self.cpu_ring["spark"].update()
      self.mem_ring["spark"].update()
      self.storage_ring["spark"].update()
      for card in self._node_cards.values():
        card.refresh_theme()
      for win in self._node_windows.values():
        win.refresh_theme()