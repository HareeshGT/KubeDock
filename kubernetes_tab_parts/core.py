from .common import *


class KubernetesCoreMixin:
  def _context_flag(self) -> str:
    context = (self._current_context or "").strip()
    return f"--context={shlex.quote(context)}" if context else ""


  def _apply_context_to_command(self, cmd: str) -> str:
    """Apply the selected context to a kubectl command."""
    if not cmd or not self._current_context:
      return cmd
    stripped = cmd.lstrip()
    if not re.match(r"^kubectl(?:\s|$)", stripped):
      return cmd
    if re.search(r"(?:^|\s)--context(?:=|\s)", stripped):
      return cmd
    prefix_len = len(cmd) - len(stripped)
    return cmd[:prefix_len] + "kubectl " + self._context_flag() + stripped[len("kubectl"): ]


  def _load_contexts(self):
    """Load kubeconfig contexts, but do not start resource queries yet.

    A machine may have kubectl/kubeconfig installed while the configured
    cluster is unavailable. In that case we only do the context lookup
    and one cluster-health probe; resource tabs remain completely idle.
    """
    self._cluster_available = False
    self._cluster_probe_in_progress = False
    self._auto_refresh_timer.stop()
    self._run_cmd(
      "kubectl config get-contexts -o name",
      self._populate_contexts,
      apply_context=False,
    )


  def _populate_contexts(self, out: str):
    contexts = [x.strip() for x in (out or "").splitlines() if x.strip()]
    self._contexts = contexts
    current = self._current_context

    self.context_combo.blockSignals(True)
    self.context_combo.clear()
    self.context_combo.addItems(contexts)
    chosen = current if current in contexts else (contexts[0] if contexts else "")
    if chosen:
      self.context_combo.setCurrentText(chosen)
      self._current_context = chosen
      # setCurrentText() above is wrapped in blockSignals(), so
      # currentTextChanged (and therefore _on_context_change /
      # context_changed) never fires for this initial pick — only for a
      # later, user-driven switch. Without this, anything listening for
      # context_changed (e.g. DashboardTab.set_kube_context, wired up in
      # main_window.py) never learns which cluster was actually selected
      # on first connect, and silently falls back to querying whatever
      # the SSH session's ambient kubectl current-context happens to be
      # — which can be a different cluster than the one shown here.
      self.context_changed.emit(chosen)
    self.context_combo.blockSignals(False)

    if not self._current_context:
      self._cluster_available = False
      self._current_ns = ""
      self.ns_combo.clear()
      self.health_lbl.setText("● No Kubernetes Cluster Found")
      self.health_lbl.setStyleSheet(f"color: {T['WARNING']}; font-size: 12px;")
      return

    self.health_lbl.setText(f"● Checking")
    self.health_lbl.setStyleSheet(f"color: {T['WARNING']}; font-size: 12px;")
    self._check_cluster_health()


  def _on_context_change(self, context: str):
    context = (context or "").strip()
    if not context or context == self._current_context:
      return

    self._cluster_available = False
    self._cluster_probe_in_progress = False
    self._auto_refresh_timer.stop()
    self._current_context = context
    self.context_changed.emit(context)
    self._current_ns = "default"
    self.ns_combo.blockSignals(True)
    self.ns_combo.clear()
    self.ns_combo.addItem("Loading…")
    self.ns_combo.blockSignals(False)
    self.health_lbl.setText(f"● Checking")
    self.health_lbl.setStyleSheet(f"color: {T['WARNING']}; font-size: 12px;")
    self._check_cluster_health()

  # ── Local connection info (for tunnelling) ────────────────

  def set_connection_info(self, host, port, user, pem):
    self._conn_host = host
    self._conn_port = port or 22
    self._conn_user = user
    self._conn_pem = pem


  def clear_connection_info(self):
    self._stop_tunnel()
    self._conn_host = None
    self._conn_port = 22
    self._conn_user = None
    self._conn_pem = None
    self._tunnel_services = []
    self.tunnel_list.clear()
    self.tunnel_list.set_empty_text("Connect to a VM to see its tunnel services")
    self.tunnel_cmd_preview.clear()

  # ── SSH wiring ────────────────────────────────────────────

  def set_sudo_user(self, username=None):
    """Keep Kubernetes remote commands in sync with the terminal's sudo context."""
    self._sudo_user = (username or "").strip() or None

  def set_ssh(self, ssh):
    self.ssh = ssh
    if hasattr(self, "k8s_ai_ops"):
      self.k8s_ai_ops.set_ssh(ssh)

    if ssh:
      self._load_contexts()
      self._load_tunnel_csv()   # Load from this VM
    else:
      self._clear_all()

      self._tunnel_services = []
      self.tunnel_list.clear()
      self.tunnel_list.set_empty_text("Connect to a VM to see its tunnel services")
      self.tunnel_cmd_preview.clear()

  # ── UI construction ───────────────────────────────────────

  def _load_namespaces(self):
    self._run_cmd(
      "kubectl get namespaces -o jsonpath='{.items[*].metadata.name}'",
      self._populate_namespaces,
    )


  def _populate_namespaces(self, out: str):
    names = out.strip().strip("'").split()
    self._namespaces = names
    current = self.ns_combo.currentText()
    # A just-created namespace (see _create_namespace) takes priority
    # over whatever was selected before, so the picker lands on the
    # namespace that was just created instead of silently staying put.
    pending = self._namespaces_pending_select
    self._namespaces_pending_select = None
    self.ns_combo.blockSignals(True)
    self.ns_combo.clear()
    self.ns_combo.addItem("(all namespaces)")
    self.ns_combo.addItems(names)
    if pending and pending in names:
      self.ns_combo.setCurrentText(pending)
    elif current in names:
      self.ns_combo.setCurrentText(current)
    elif "default" in names:
      self.ns_combo.setCurrentText("default")
    self.ns_combo.blockSignals(False)
    self._current_ns = self.ns_combo.currentText()
    if self._cluster_available:
      self._refresh_current_tab()


  def _on_ns_change(self, ns: str):
    self._current_ns = ns
    self._refresh_current_tab()


  def _ns_flag(self) -> str:
    ns = self._current_ns
    if ns == "(all namespaces)" or not ns:
      return "--all-namespaces"
    return f"-n {ns}"


  def _create_namespace(self):
    if not self.ssh:
      return
    name, ok = QInputDialog.getText(self, "Create Namespace", "Namespace name:")
    name = (name or "").strip()
    if not ok or not name:
      return
    if not re.match(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", name):
      QMessageBox.warning(
        self, "Invalid name",
        "Namespace names must be lowercase alphanumeric or '-', "
        "and must start/end with an alphanumeric character."
      )
      return

    def on_done(out):
      self._log(out)
      self._namespaces_pending_select = name
      self._load_namespaces()

    self._run_cmd(f"kubectl create namespace {name} 2>&1", on_done)

  # ── Cluster health ────────────────────────────────────────

  def _check_cluster_health(self):
    """Probe the selected context before allowing any resource command."""
    if not self.ssh or not self._current_context:
      self._cluster_available = False
      return
    if self._cluster_probe_in_progress:
      return

    self._cluster_probe_in_progress = True
    self._run_cmd(
      "kubectl cluster-info --request-timeout=3s 2>&1 | head -3",
      self._update_health,
    )


  def _update_health(self, out: str):
    self._cluster_probe_in_progress = False
    text = (out or "").lower()
    healthy = (
      "running" in text
      or "control plane" in text
      or "kubernetes control plane" in text
    ) and not any(
      bad in text
      for bad in (
        "unable to connect",
        "connection refused",
        "connection timed out",
        "i/o timeout",
        "no such host",
        "context deadline exceeded",
        "the server doesn't have a resource type",
      )
    )

    self._cluster_available = healthy

    if healthy:
      self.health_lbl.setText("● Cluster OK")
      self.health_lbl.setStyleSheet(f"color: {T['SUCCESS']}; font-size: 12px;")
      self._load_namespaces()
    else:
      self._auto_refresh_timer.stop()
      self.auto_btn.setChecked(False)
      self.auto_btn.setText("⏱ Auto (30 s)")
      self.health_lbl.setText("● Cluster unavailable")
      self.health_lbl.setStyleSheet(f"color: {T['DANGER']}; font-size: 12px;")
      self._clear_all(keep_context=True)


  def _toggle_auto_refresh(self, on: bool):
    if on:
      self._auto_refresh_timer.start(30000)
      self.auto_btn.setText("⏱ Auto ON")
    else:
      self._auto_refresh_timer.stop()
      self.auto_btn.setText("⏱ Auto (30 s)")


  def _auto_refresh(self):
    if not self.ssh or not self._cluster_available or not self._current_context:
      self._auto_refresh_timer.stop()
      self.auto_btn.setChecked(False)
      self.auto_btn.setText("⏱ Auto (30 s)")
      return
    self._refresh_current_tab()


  def _clear_all(self, keep_context=False):
    self.pod_list.clear()
    self.deploy_list.clear()
    self.pod_count_lbl.setText("")
    self.pod_count_lbl.setStyleSheet("")
    self.deploy_count_lbl.setText("")
    self.deploy_count_lbl.setStyleSheet("")
    self.sts_list.clear()
    self.sts_count_lbl.setText("")
    self.sts_count_lbl.setStyleSheet("")
    self.ds_list.clear()
    self.ds_count_lbl.setText("")
    self.ds_count_lbl.setStyleSheet("")
    self.hpa_list.clear()
    self.hpa_count_lbl.setText("")
    self.hpa_count_lbl.setStyleSheet("")
    self.svc_list.clear()
    self.svc_count_lbl.setText("")
    self.svc_count_lbl.setStyleSheet("")
    self.ing_list.clear()
    self.ing_count_lbl.setText("")
    self.ing_count_lbl.setStyleSheet("")
    self.wl_list.clear()
    self.wl_history_list.clear()
    self.wl_count_lbl.setText("")
    self.wl_count_lbl.setStyleSheet("")
    self.pvx_list.clear()
    self.pvx_count_lbl.setText("")
    self.pvx_count_lbl.setStyleSheet("")
    self.cfg_list.clear()
    self.cfg_detail.clear()
    self.cfg_raw.clear()
    self.event_list.clear()
    self.event_count_lbl.setText("")
    self.event_count_lbl.setStyleSheet("")
    self._events_raw = ""
    if getattr(self, "event_warn_btn", None) is not None:
      self.event_warn_btn.setChecked(False)
    self.ns_combo.clear()
    if not keep_context:
      self.context_combo.clear()
      self._contexts = []
      self._current_context = ""
    self.health_lbl.setText("● Cluster")
    self.health_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")


  def _refresh_current_tab(self, _=None):
    # Never issue resource-level kubectl commands until the selected
    # context has passed the cluster-health probe. This also prevents
    # tab changes and auto-refresh from generating unwanted commands on
    # machines that have no reachable Kubernetes cluster.
    if not self.ssh or not self._cluster_available or not self._current_context:
      return

    idx = self.sub_tabs.currentIndex()
    if  idx == 0: self._load_pods()
    elif idx == 1: self._load_deployments()
    elif idx == 2: self._load_statefulsets()
    elif idx == 3: self._load_daemonsets()
    elif idx == 4: self._load_hpas()
    elif idx == 5: self._load_services()
    elif idx == 6: self._load_ingress()
    elif idx == 7: self._load_workloads()
    elif idx == 8: self._load_storage()
    elif idx == 9: self._load_config_resources()
    elif idx == 10: self._load_events()
    elif idx == 11: self._refresh_tunnel_status()

  # ── Pods ──────────────────────────────────────────────────
  # `kubectl get pods -o wide` renders the RESTARTS column as a plain
  # number ("0") normally, but as "N (Ndhm ago)" — a single logical
  # value containing a space — for any pod whose last restart was
  # recent enough for kubectl to bother annotating it. line.split()
  # blows that annotation into two extra whitespace-separated tokens
  # ("(22d", "ago)"), which silently shifts every fixed-position column
  # after it (AGE/IP/NODE) by two — the misalignment seen when a
  # recently-restarted pod's IP/Node show up empty or wrong while an
  # untouched pod in the same table lines up fine. _split_pod_line
  # detects that two-token annotation before the fixed-offset slicing
  # below runs, pulls it out into its own "Last Restart" value (rather
  # than just discarding it — it's genuinely useful info), and returns
  # the remaining tokens so the real columns land back in place.
  _PAREN_OPEN_RE = re.compile(r"^\(\S*$")
  _PAREN_CLOSE_RE = re.compile(r"^\S*\)$")


  def _run_kubectl(self):
    cmd = self.kubectl_inp.text().strip()
    if not cmd:
      return
    if not cmd.startswith("kubectl"):
      cmd = "kubectl " + cmd
    self.kubectl_inp.clear()
    self._run_cmd(cmd + " 2>&1",
           lambda o: (self._log(o), self.sub_tabs.setCurrentIndex(5)))


  def _run_kubectl_terminal(self):
    cmd = self.k8s_inp.text().strip()
    if not cmd:
      return
    if not cmd.startswith("kubectl"):
      cmd = "kubectl " + cmd
    self._append_html(f"\n<span style='color:{T['ACCENT2']}'>$ {self._esc(cmd)}</span>")
    self._run_cmd(cmd + " 2>&1", lambda o: self._append_pre(o))
    self.k8s_inp.clear()

  # ── Port tunnels ──────────────────────────────────────────
  # ── Status-column glyphs ───────────────────────────────────
  STATUS_UNKNOWN = ""
  STATUS_UP   = ""
  STATUS_DOWN  = ""

  # Per-item data role storing the last-known exposed state (True/False),
  # or None before the first status check completes. Read by
  # _filter_tunnel_services() to apply the Active/Inactive toggle.
  TUNNEL_STATUS_ROLE = Qt.UserRole + 1


  def _describe(self, kind: str, name: str, ns: str):
    self._describe_title = f"describe {kind}/{name}"
    self._run_cmd(f"kubectl describe {kind} {name} -n {ns} 2>&1",
           self._show_describe_dialog)


  def _show_describe_dialog(self, out: str):
    dlg = QDialog(self)
    dlg.setWindowTitle(getattr(self, "_describe_title", "Describe"))
    dlg.resize(900, 620)
    apply_qss_to(dlg)
    lay = _QVL(dlg)
    te = QTextEdit()
    te.setReadOnly(True)
    te.setFont(monospace_font(11))
    te.setPlainText(out)
    lay.addWidget(te)
    bb = QDialogButtonBox(QDialogButtonBox.Close)
    bb.rejected.connect(dlg.reject)
    lay.addWidget(bb)
    dlg.exec_()

  # ── Terminal output helpers ───────────────────────────────

  @staticmethod
  def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
          .replace(">", "&gt;"))


  def _append_html(self, html: str):
    """Append raw HTML (e.g. the colored '$ cmd' line)."""
    append_terminal_html(self.k8s_terminal, html)


  def _append_pre(self, text: str):
    """Append plain command output, preserving whitespace so columns don't zigzag."""
    append_terminal_text(self.k8s_terminal, text)

  # ── Terminal tab ──────────────────────────────────────────

  def _run_cmd(self, cmd: str, callback, apply_context: bool = True, on_error=None, sudo_user=None):
    if not self.ssh:
      return
    if apply_context:
      cmd = self._apply_context_to_command(cmd)
    self.progress.show()
    worker = CommandWorker(self.ssh, cmd, sudo_user=sudo_user or getattr(self, "_sudo_user", None))

    def on_done(out):
      self.progress.hide()
      callback(out)

    def on_error(e):
      self.progress.hide()
      self._log(f"[error] {e}")
      if on_error:
        on_error(e)

    worker.done.connect(on_done)
    worker.error.connect(on_error)
    track_worker(self._workers, worker)
    worker.start()


  def _log(self, text: str):
    self._append_pre(text)
    self.status_msg.emit(text.split("\n")[0][:80])
  def _style_tree(self, tree):
    font = monospace_font(13)
    tree.setFont(font)

    tree.setStyleSheet("""
    QTreeWidget {
      font-size: 13px;
    }

    QTreeWidget::item {
      height: 38px;
    }
    """)

    hdr = tree.header()
    header_font = QFont("Segoe UI", 12)
    header_font.setBold(True)
    hdr.setFont(header_font)
    hdr.setMinimumHeight(42)


  def _style_context_group(self):
    self.context_group.setStyleSheet(
      f"QWidget#context_group {{ background: {T['BG_ITEM']}; border: 1px solid {T['BORDER']}; border-radius: 20px; }}"
    )
    self.context_combo.setStyleSheet(
      f"QComboBox#context_combo {{ background: {T['BG_PANEL']}; color: {T['TEXT_PRIMARY']}; border: 1.5px solid {T['ACCENT2']}; border-radius: 15px; padding: 2px 30px 2px 14px; font-size: 13px; font-weight: 600; min-width: 190px; }}"
      f"QComboBox#context_combo:hover {{ border-color: {T['ACCENT']}; background: {T['BG_HOVER']}; }}"
      f"QComboBox#context_combo::drop-down {{ border: none; width: 26px; }}"
    )


  def _style_ns_group(self):
    """Pill chip around the namespace picker + a bigger, bolder combo
    box than the app-wide default. Set directly on the two widgets
    (rather than in themes.py's global QComboBox rule) so every other
    dropdown in the app keeps its normal size — only this one, the
    most-used control on the tab, gets the larger treatment. Re-called
    from apply_theme() on every theme switch since the colours below
    are baked in as literal hex at call time."""
    self._style_context_group()
    self.ns_group.setStyleSheet(
      f"QWidget#ns_group {{ background: {T['BG_ITEM']}; "
      f"border: 1px solid {T['BORDER']}; border-radius: 20px; }}"
    )
    self.ns_combo.setStyleSheet(
      f"QComboBox#ns_combo {{ background: {T['BG_PANEL']}; color: {T['TEXT_PRIMARY']}; "
      f"border: 1.5px solid {T['ACCENT']}; border-radius: 15px; "
      f"padding: 2px 30px 2px 14px; font-size: 13px; font-weight: 600; min-width: 190px; }}"
      f"QComboBox#ns_combo:hover {{ border-color: {T['ACCENT2']}; background: {T['BG_HOVER']}; }}"
      f"QComboBox#ns_combo::drop-down {{ border: none; width: 26px; }}"
      f"QComboBox#ns_combo::down-arrow {{ width: 10px; height: 10px; }}"
    )


  def visible_tab_titles(self) -> list:
    """The exact tab-bar strings currently in sub_tabs, in order —
    used by SettingsDialog to build its show/hide checklist and as
    the stable keys stored in the SQLite app_settings table."""
    return [self.sub_tabs.tabText(i) for i in range(self.sub_tabs.count())]


  def apply_hidden_tabs(self, hidden_titles):
    """Hide/show sub-tabs by title. Safe to call at any time (e.g.
    right after the user saves new choices in Settings) — QTabWidget
    keeps a hidden tab's contents alive, it just isn't selectable
    from the tab bar."""
    hidden = set(hidden_titles or [])
    for i in range(self.sub_tabs.count()):
      self.sub_tabs.setTabVisible(i, self.sub_tabs.tabText(i) not in hidden)


  def _apply_saved_hidden_tabs(self):
    self.apply_hidden_tabs(load_settings().get("k8s_hidden_tabs", []))

  # ── Theme refresh ─────────────────────────────────────────

  def apply_theme(self):
    self.ctrl_bar.setStyleSheet(
      f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
    )
    self.health_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
    self.ns_dot.setStyleSheet(f"color: {T['ACCENT']}; font-size: 11px; background: transparent;")
    self._style_ns_group()
    if hasattr(self, "pod_action_cluster"):
      self.pod_action_cluster.setStyleSheet(
        f"QFrame#action_cluster {{ background: {T['BG_ITEM']}; border-radius: 8px; }}"
      )
    self.k8s_terminal.setStyleSheet(
      f"background: #0d0d1a; color: {T['SUCCESS']}; border: none; padding: 8px;"
    )
    toolbar_style = f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
    for bar in (getattr(self, "pods_toolbar", None), getattr(self, "deploy_toolbar", None),
          getattr(self, "sts_toolbar", None), getattr(self, "ds_toolbar", None),
          getattr(self, "hpa_toolbar", None),
          getattr(self, "svc_toolbar", None), getattr(self, "ing_toolbar", None),
          getattr(self, "events_toolbar", None), getattr(self, "tunnel_toolbar", None),
          getattr(self, "wl_toolbar", None), getattr(self, "wl_type_bar", None),
          getattr(self, "pvx_toolbar", None), getattr(self, "pvx_type_bar", None)):
      if bar is not None:
        bar.setStyleSheet(toolbar_style)
    if getattr(self, "wl_type_toggle", None) is not None:
      self._style_toggle(self.wl_type_toggle, (self.wl_type_jobs_btn, self.wl_type_cron_btn))
    if getattr(self, "pvx_type_toggle", None) is not None:
      self._style_toggle(self.pvx_type_toggle, (self.pvx_type_pvc_btn, self.pvx_type_pv_btn))
    if getattr(self, "wl_history_hdr", None) is not None:
      self.wl_history_hdr.setStyleSheet(
        f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
        f"font-weight: 700; border-bottom: 1px solid {T['BORDER']}; padding-left: 14px;"
      )
    if getattr(self, "wl_history_hint", None) is not None:
      self.wl_history_hint.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px; padding: 12px;")
    if getattr(self, "tunnel_log", None) is not None:
      self.tunnel_log.setStyleSheet(
        f"background: #0d0d1a; color: {T['TEXT_DIM']}; border: none; padding: 8px;"
      )
    if getattr(self, "tunnel_list", None) is not None:
      self.tunnel_list.refresh_theme()
    if getattr(self, "tunnel_path_lbl", None) is not None:
      self.tunnel_path_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px;")
    if getattr(self, "tunnel_status_lbl", None) is not None:
      running = self._tunnel_process is not None and self._tunnel_process.state() != QProcess.NotRunning
      color = T['SUCCESS'] if running else T['TEXT_MUTED']
      self.tunnel_status_lbl.setStyleSheet(f"color: {color}; font-size: 12px;")
    if getattr(self, "cfg_detail_hdr", None) is not None:
      self.cfg_detail_hdr.setStyleSheet(
        f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
        f"font-weight: 700; border-bottom: 1px solid {T['BORDER']}; padding-left: 14px;"
      )
    if getattr(self, "cfg_type_bar", None) is not None:
      self.cfg_type_bar.setStyleSheet(
        f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
      )
    if getattr(self, "cfg_type_toggle", None) is not None:
      self._style_cfg_toggle()
    if getattr(self, "cfg_raw", None) is not None:
      self.cfg_raw.setStyleSheet(f"padding: 10px; border: none; background: {T['BG_DARK']};")
    if getattr(self, "cfg_raw_lbl", None) is not None:
      self.cfg_raw_lbl.setStyleSheet(
        f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
        f"font-weight: 700; border-top: 1px solid {T['BORDER']}; "
        f"border-bottom: 1px solid {T['BORDER']}; padding-left: 14px;"
      )
    if self.ssh and self._current_context:
      self._check_cluster_health()
      # Pod/deployment cards (k8s_cards.py) bake T's colors in at
      # construction time rather than re-reading them live, so a
      # theme switch needs a rebuild of whichever list is on screen
      # for its cards to pick up the new palette.
      self._refresh_current_tab()


  def _set_count_badge(self, lbl: QLabel, text: str, color_key: str = "TEXT_DIM"):
    """Style a QLabel as a small pill badge, matching the card badges
    in k8s_cards.py, and set its text in one call."""
    color = T.get(color_key, T["TEXT_DIM"])
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    lbl.setText(text)
    lbl.setStyleSheet(
      f"background: rgba({r},{g},{b},0.15); color: {color}; "
      f"border: 1px solid rgba({r},{g},{b},0.4); border-radius: 9px; "
      f"padding: 3px 10px; font-size: 12px; font-weight: 700;"
    )


  def _toolbar_btn(self, label: str, object_name: str = None, tooltip: str = "") -> QPushButton:
    """Build a toolbar action button with a uniform, fixed shape.

    Buttons here mix plain text with emoji glyphs (" Logs", " Tunnel",
    "Run", …). Emoji fall back to a different font than the rest of the
    label, and that fallback font's line-height isn't the same as
    'Segoe UI' — so without a fixed height, buttons with an emoji end up
    a few px taller than plain-text ones, and the shared border-radius
    then reads as visually different corner shapes across the toolbar.
    Forcing every button through this one helper keeps height, padding,
    and radius identical everywhere regardless of label content.
    """
    btn = QPushButton()
    apply_text_icon(btn, label)
    if object_name:
      btn.setObjectName(object_name)
    if tooltip:
      btn.setToolTip(tooltip)
    btn.setFixedHeight(32)
    btn.setStyleSheet("padding: 0 14px;")
    return btn


  def _vline(self):
    f = QFrame()
    f.setFrameShape(QFrame.VLine)
    f.setStyleSheet(f"color: {T['BORDER']};")
    f.setFixedWidth(1)
    return f


