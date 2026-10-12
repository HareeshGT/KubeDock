from .common import *
from k8s_metrics import (
  build_pod_metrics, build_pods_command, parse_limits_output,
  parse_top_output, split_pod_sections,
)


_PAREN_OPEN_RE = re.compile(r"^\(\S*$")
_PAREN_CLOSE_RE = re.compile(r"^\S*\)$")


class KubernetesResourcesMixin:
  @staticmethod
  def _split_pod_line(line: str):
    """Returns (cleaned_parts, last_restart). last_restart is e.g.
    "22d ago", or "" if this pod has never restarted (or kubectl's
    RESTARTS column didn't include the annotation)."""
    parts = line.split()
    cleaned = []
    last_restart = ""
    i = 0
    while i < len(parts):
      if (_PAREN_OPEN_RE.match(parts[i]) and i + 1 < len(parts)
          and _PAREN_CLOSE_RE.match(parts[i + 1])):
        # "(22d" + "ago)" -> "22d ago"
        last_restart = f"{parts[i][1:]} {parts[i + 1][:-1]}"
        i += 2
        continue
      cleaned.append(parts[i])
      i += 1
    return cleaned, last_restart


  def _pods_request_key(self):
    return (self._current_context, self._current_ns)


  def _load_pods(self):
    """One refresh = ONE worker/SSH command: the pod list plus the
    `kubectl top pods` usage and container limits behind each card's
    CPU/memory indicators (see k8s_metrics.build_pods_command). Requests
    never overlap: a call made while one is in flight (auto-refresh timer,
    manual refresh, namespace change, a pod action) is coalesced into a
    single follow-up refresh once the current one lands."""
    if not self.ssh:
      return
    if self._pods_load_inflight:
      self._pods_reload_pending = True
      return
    self._pods_load_inflight = True
    self._pods_reload_pending = False
    key = self._pods_request_key()
    cmd = build_pods_command(self._ns_flag(), self._context_flag())
    # The context flag is already on every kubectl call in `cmd`;
    # apply_context would only patch the first one.
    self._run_cmd(cmd, lambda out: self._on_pods_loaded(key, out),
                  apply_context=False, on_error=self._on_pods_load_failed)


  def _on_pods_loaded(self, key, out: str):
    self._pods_load_inflight = False  # first, so a failure below can't wedge refreshes
    same_view = (key == self._pods_request_key())
    if same_view:
      self._populate_pods(out)
    # A different namespace/context was picked meanwhile (the response is
    # stale and not shown), or another refresh was requested: run it now.
    if self._pods_reload_pending or not same_view:
      self._load_pods()


  def _on_pods_load_failed(self, err):
    # _run_cmd has already logged the error; just free the slot, and honour
    # a refresh that was requested while this one was running.
    self._pods_load_inflight = False
    if self._pods_reload_pending:
      self._load_pods()


  def _populate_pods(self, out: str):
    self.pod_list.clear()
    all_ns = (self._current_ns == "(all namespaces)")
    total  = 0
    running = 0
    # `out` is the combined refresh output; split it into the pod listing
    # and the usage / limits sections, parsed once for the whole list.
    pods_text, top_text, limits_text = split_pod_sections(out)
    usage, usage_problem = parse_top_output(top_text, self._current_ns or "default")
    limits = parse_limits_output(limits_text)
    # The namespace chip on each card is only shown in "(all namespaces)"
    # view (i.e. rows can differ) — when one namespace is selected it's
    # implied by ns_combo already, so the chip would just repeat itself.
    for line in pods_text.strip().splitlines()[1:]:
      parts, last_restart = self._split_pod_line(line)
      if all_ns:
        # `kubectl get pods --all-namespaces -o wide` prepends NAMESPACE.
        if len(parts) < 6:
          continue
        ns, name, ready, status, restarts, age = parts[:6]
        ip  = parts[6] if len(parts) > 6 else "-"
        node = parts[7] if len(parts) > 7 else "-"
      else:
        if len(parts) < 5:
          continue
        ns = self._current_ns or "default"
        name, ready, status, restarts, age = parts[:5]
        ip  = parts[5] if len(parts) > 5 else "-"
        node = parts[6] if len(parts) > 6 else "-"
      meta = {
        "namespace": ns, "name": name, "ready": ready, "status": status,
        "restarts": restarts, "last_restart": last_restart or "-",
        "age": age, "ip": ip, "node": node,
      }
      item = QListWidgetItem()
      item.setData(Qt.UserRole, meta)
      item.setSizeHint(QSize(0, PodCardWidget.CARD_HEIGHT))
      self.pod_list.addItem(item)
      # Matched on (namespace, name): identical pod names in different
      # namespaces must never share usage figures.
      card = PodCardWidget(meta, all_ns, metrics=build_pod_metrics(
        (ns, name), usage, limits, usage_problem))
      card.ai_requested.connect(self._on_pod_card_ai_requested)
      self.pod_list.setItemWidget(item, card)
      if meta.get("name") == self._pod_ai_pod:
        # A refresh landed while this pod's diagnosis was still in
        # flight — the old card (and its " …" busy state) just got
        # thrown away, so re-point the busy state at its replacement.
        self._pod_ai_card = card
        card.set_ai_busy(True)
      total += 1
      if "running" in status.lower():
        running += 1
    if total == 0:
      color_key = "TEXT_MUTED"
    elif running == total:
      color_key = "SUCCESS"
    elif running == 0:
      color_key = "DANGER"
    else:
      color_key = "WARNING"
    self._set_count_badge(self.pod_count_lbl, f"{running}/{total} running", color_key)
    # Refreshing rebuilds every row from scratch, which would otherwise
    # silently show everything again even though the filter box still
    # has text in it — reapply whatever's currently typed there.
    self._filter_pods(self.pod_filter.text())


  def _filter_pods(self, text: str):
    q = text.lower()
    for i in range(self.pod_list.count()):
      item = self.pod_list.item(i)
      meta = item.data(Qt.UserRole) or {}
      item.setHidden(q not in meta.get("name", "").lower())


  def _selected_pod(self) -> tuple: # (Optional[str], str)
    item = self.pod_list.currentItem()
    if not item:
      QMessageBox.warning(self, "No selection", "Select a pod first.")
      return None, ""
    meta = item.data(Qt.UserRole) or {}
    return meta.get("name"), meta.get("namespace") or "default"


  def _on_pod_double_click(self, item):
    """Double-clicking a pod card is a shortcut for Describe — reads
    name/namespace off the card that was actually double-clicked rather
    than relying on _selected_pod()'s currentItem(), since a
    double-click's second press is what sets the current item and
    there's no reason to depend on that timing."""
    meta = item.data(Qt.UserRole) or {}
    self._describe("pod", meta.get("name"), meta.get("namespace") or "default")


  def _on_pod_selection_changed(self, current, previous):
    """Cards paint their own selected state (they fully cover the
    QListWidgetItem's rect, so the list's native selection styling
    never shows through) — forward selection changes into them."""
    if previous is not None:
      w = self.pod_list.itemWidget(previous)
      if w:
        w.set_selected(False)
    if current is not None:
      w = self.pod_list.itemWidget(current)
      if w:
        w.set_selected(True)


  def _pod_logs(self):
    pod, ns = self._selected_pod()
    if pod:
      self._logs_pod(pod, ns)



  def _logs_pod(self, pod: str, ns: str):
    """Look up the pod containers before opening LogViewerDialog.

    Single-container pods open directly. Multi-container pods require an
    explicit container selection so logs are never taken from the wrong
    sidecar/container by accident.
    """
    self._run_cmd(
      f"kubectl get pod -n {ns} {pod} "
      f"-o jsonpath='{{.spec.containers[*].name}}' 2>&1",
      lambda out, pod=pod, ns=ns:
        self._on_logs_containers_fetched(out, pod, ns),
    )



  def _on_logs_containers_fetched(self, out: str, pod: str, ns: str):
    # Same trailing-quote defensiveness as the Exec container lookup.
    containers = out.strip().strip("'").split()

    if len(containers) <= 1:
      LogViewerDialog(
        self,
        self.ssh,
        ns,
        pod,
        containers[0] if containers else None
      ).exec_()
      return

    dlg = ContainerPickerDialog(
      self,
      pod,
      containers,
      action="Logs"
    )

    if dlg.exec_() == QDialog.Accepted:
      LogViewerDialog(
        self,
        self.ssh,
        ns,
        pod,
        dlg.selected_container()
      ).exec_()

  # ── Inline " AI" button on troubled pod cards ─────────────
  # Same diagnosis flow as LogViewerDialog's "Analyze with AI" button
  # (fetch logs -> ai_assist.AIExplainWorker -> AIExplainDialog), just
  # entered straight from the card instead of requiring the user to open
  # the full log viewer first. Single-flight: only one card's request
  # runs at a time (see the self._pod_ai_* state in __init__).

  def _on_pod_card_ai_requested(self, meta: dict):
    if self._pod_ai_pod is not None:
      return # a diagnosis is already running — button is disabled meanwhile, but be defensive

    if not self.ssh:
      QMessageBox.warning(self, "Not connected", "Connect to the instance first.")
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

    pod  = meta.get("name")
    ns   = meta.get("namespace") or "default"
    status = meta.get("status", "") or ""

    self._pod_ai_pod = pod
    self._pod_ai_card = self.sender() if isinstance(self.sender(), PodCardWidget) else None
    if self._pod_ai_card is not None:
      self._pod_ai_card.set_ai_busy(True)

    self._pod_ai_dialog = AIExplainDialog(self, f"AI diagnosis — {pod}")
    self._pod_ai_dialog.body.setPlainText("Fetching logs…")
    self._pod_ai_dialog.show()

    # A pod that's actively crash-looping usually has nothing useful in
    # its *current* container's logs (it just restarted) — the actual
    # error is in the previous container's log instead. A pod with
    # restarts>0 but currently Running already fell back to this same
    # heuristic on the card itself (see k8s_cards._pod_in_trouble), so
    # mirror it here rather than re-deriving it from restarts alone.
    use_previous = "crash" in status.lower()
    prev = "--previous" if use_previous else ""
    inner = f"kubectl {self._context_flag()} logs --tail=200 -n {ns} {prev} {pod} 2>&1".replace("kubectl logs", "kubectl logs")
    cmd = f"bash -lc {shlex.quote(inner)}"

    worker = CommandWorker(self.ssh, cmd)
    worker.done.connect(lambda out, pod=pod, ns=ns: self._on_pod_ai_logs_fetched(pod, ns, out))
    worker.error.connect(lambda err: self._on_pod_ai_logs_error(err))
    self._pod_ai_log_worker = worker
    track_worker(self._workers, worker)
    worker.start()


  def _on_pod_ai_logs_fetched(self, pod: str, ns: str, log_text: str):
    self._pod_ai_log_worker = None
    log_text = (log_text or "").strip()
    if not log_text:
      self._on_pod_ai_logs_error("No log output for this pod.")
      return

    if self._pod_ai_dialog is not None:
      self._pod_ai_dialog.set_source_context(
        f"Pod: {pod}\nNamespace: {ns}\n\nLogs/evidence:\n{log_text}"
      )
      self._pod_ai_dialog.set_loading()

    provider = ai_assist.get_provider()
    worker = ai_assist.AIExplainWorker(
      provider, ai_assist.get_api_key(provider), ai_assist.get_model(provider),
      pod, ns, None, log_text,
    )
    worker.done.connect(self._on_pod_ai_done)
    worker.error.connect(self._on_pod_ai_error)
    worker.finished.connect(self._on_pod_ai_finished)
    self._pod_ai_worker = worker
    worker.start()


  def _on_pod_ai_logs_error(self, message: str):
    self._pod_ai_log_worker = None
    if self._pod_ai_dialog is not None:
      self._pod_ai_dialog.set_error(f"Couldn't fetch logs: {message}")
    self._on_pod_ai_finished()


  def _on_pod_ai_done(self, text: str):
    if self._pod_ai_dialog is not None:
      self._pod_ai_dialog.set_markdown(text)


  def _on_pod_ai_error(self, message: str):
    if self._pod_ai_dialog is not None:
      self._pod_ai_dialog.set_error(message)


  def _on_pod_ai_finished(self):
    self._pod_ai_worker = None
    self._pod_ai_pod  = None
    if self._pod_ai_card is not None:
      self._pod_ai_card.set_ai_busy(False)
    self._pod_ai_card = None


  def _pod_exec(self):
    pod, ns = self._selected_pod()
    if pod:
      self._exec_pod(pod, ns)


  def _exec_pod(self, pod: str, ns: str):
    """Look up the pod's container names before opening ExecDialog —
    see ContainerPickerDialog's docstring for why. Single-container
    pods (the common case) skip straight to ExecDialog with no extra
    click."""
    self._run_cmd(
      f"kubectl get pod -n {ns} {pod} "
      f"-o jsonpath='{{.spec.containers[*].name}}' 2>&1",
      lambda out, pod=pod, ns=ns: self._on_exec_containers_fetched(out, pod, ns),
    )


  def _on_exec_containers_fetched(self, out: str, pod: str, ns: str):
    # Same trailing-quote defensiveness as _populate_namespaces.
    containers = out.strip().strip("'").split()
    if len(containers) <= 1:
      ExecDialog(self, self.ssh, ns, pod, containers[0] if containers else None,
            context=self._current_context).exec_()
      return
    dlg = ContainerPickerDialog(self, pod, containers)
    if dlg.exec_() == QDialog.Accepted:
      ExecDialog(self, self.ssh, ns, pod, dlg.selected_container(),
            context=self._current_context).exec_()


  def _pod_delete(self):
    pod, ns = self._selected_pod()
    if not pod:
      return
    if QMessageBox.question(self, "Delete Pod", f'Delete pod "{pod}"?',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
      self._run_cmd(f"kubectl delete pod -n {ns} {pod} 2>&1",
             lambda o: (self._log(o), self._load_pods()))


  def _pod_restart(self):
    pod, ns = self._selected_pod()
    if pod:
      self._run_cmd(f"kubectl delete pod -n {ns} {pod} 2>&1",
             lambda o: (self._log(o), self._load_pods()))


  def _pod_ctx_menu(self, pos):
    item = self.pod_list.itemAt(pos)
    if not item:
      return
    self.pod_list.setCurrentItem(item)
    meta = item.data(Qt.UserRole) or {}
    pod = meta.get("name")
    ns  = meta.get("namespace") or "default"
    menu = QMenu(self)
    menu.addAction(" View Logs", lambda: self._logs_pod(pod, ns))
    menu.addAction(" Exec Shell", lambda: self._exec_pod(pod, ns))
    menu.addAction(" Describe",  lambda: self._describe("pod", pod, ns))
    menu.addSeparator()
    menu.addAction(" Delete", self._pod_delete)
    menu.exec_(self.pod_list.viewport().mapToGlobal(pos))

  # ── Deployments ───────────────────────────────────────────

  def _load_deployments(self):
    self._run_cmd(f"kubectl get deployments {self._ns_flag()} -o wide 2>&1",
           self._populate_deployments)


  def _populate_deployments(self, out: str):
    self.deploy_list.clear()
    all_ns = (self._current_ns == "(all namespaces)")
    total = 0
    ready_count = 0
    for line in out.strip().splitlines()[1:]:
      parts = line.split()
      if all_ns:
        # `kubectl get deployments --all-namespaces -o wide` prepends
        # NAMESPACE — without accounting for it, every column below
        # silently shifts left by one (Name shows the namespace,
        # Ready shows the name, and so on).
        if len(parts) < 6:
          continue
        ns, name, ready, upd, avail, age = parts[:6]
        imgs = " | ".join(parts[6:]) if len(parts) > 6 else "-"
      else:
        if len(parts) < 5:
          continue
        ns = self._current_ns or "default"
        name, ready, upd, avail, age = parts[:5]
        imgs = " | ".join(parts[5:]) if len(parts) > 5 else "-"
      meta = {
        "namespace": ns, "name": name, "ready": ready,
        "up_to_date": upd, "available": avail, "age": age, "images": imgs,
      }
      item = QListWidgetItem()
      item.setData(Qt.UserRole, meta)
      item.setSizeHint(QSize(0, DeploymentCardWidget.CARD_HEIGHT))
      self.deploy_list.addItem(item)
      self.deploy_list.setItemWidget(item, DeploymentCardWidget(meta, all_ns))
      total += 1
      try:
        cur, desired = ready.split("/")
        if cur == desired:
          ready_count += 1
      except Exception:
        pass
    if total == 0:
      color_key = "TEXT_MUTED"
    elif ready_count == total:
      color_key = "SUCCESS"
    elif ready_count == 0:
      color_key = "DANGER"
    else:
      color_key = "WARNING"
    self._set_count_badge(self.deploy_count_lbl, f"{total} deployment{'s' if total != 1 else ''} · {ready_count} ready", color_key)
    # Same reasoning as _populate_pods: rebuild wipes the visual filter
    # state even though the filter box still has text — reapply it.
    self._filter_deployments(self.deploy_filter.text())


  def _on_deploy_click(self, item):
    meta = item.data(Qt.UserRole) or {}
    try:
      _, desired = meta.get("ready", "").split("/")
      self.scale_spin.setValue(int(desired))
    except Exception:
      pass


  def _on_deploy_double_click(self, item):
    """Double-clicking a deployment card is a shortcut for Describe —
    reads name/namespace off the card that was actually double-clicked,
    same reasoning as _on_pod_double_click above."""
    meta = item.data(Qt.UserRole) or {}
    self._describe("deployment", meta.get("name"), meta.get("namespace") or "default")


  def _on_deploy_selection_changed(self, current, previous):
    """Same reasoning as _on_pod_selection_changed above."""
    if previous is not None:
      w = self.deploy_list.itemWidget(previous)
      if w:
        w.set_selected(False)
    if current is not None:
      w = self.deploy_list.itemWidget(current)
      if w:
        w.set_selected(True)


  def _filter_deployments(self, text: str):
    q = text.lower()
    for i in range(self.deploy_list.count()):
      item = self.deploy_list.item(i)
      meta = item.data(Qt.UserRole) or {}
      item.setHidden(q not in meta.get("name", "").lower())


  def _selected_deploy(self) -> tuple: # (Optional[str], str)
    item = self.deploy_list.currentItem()
    if not item:
      QMessageBox.warning(self, "No selection", "Select a deployment first.")
      return None, ""
    meta = item.data(Qt.UserRole) or {}
    return meta.get("name"), meta.get("namespace") or "default"


  def _deploy_scale(self):
    dep, ns = self._selected_deploy()
    if not dep:
      return
    replicas = self.scale_spin.value()
    if QMessageBox.question(self, "Scale", f'Scale "{dep}" to {replicas} replica(s)?',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
      self._run_cmd(f"kubectl scale deployment {dep} -n {ns} --replicas={replicas} 2>&1",
             lambda o: (self._log(o), self._load_deployments()))


  def _deploy_scale_step(self, delta: int):
    """Quick +1/-1 scale, applied immediately (no confirmation dialog —
    this is the fast stepper next to the Replicas spinbox, distinct
    from the "⇅ Scale" button which jumps straight to whatever number
    is typed into the spinbox). Keeps the spinbox in sync so both
    controls always agree on the current target."""
    dep, ns = self._selected_deploy()
    if not dep:
      return
    replicas = max(self.scale_spin.minimum(),
            min(self.scale_spin.maximum(), self.scale_spin.value() + delta))
    self.scale_spin.setValue(replicas)
    self._run_cmd(f"kubectl scale deployment {dep} -n {ns} --replicas={replicas} 2>&1",
           lambda o: (self._log(o), self._load_deployments()))


  def _deploy_restart(self):
    dep, ns = self._selected_deploy()
    if dep:
      self._run_cmd(f"kubectl rollout restart deployment/{dep} -n {ns} 2>&1",
             lambda o: (self._log(o), self._load_deployments()))


  def _deploy_describe(self):
    dep, ns = self._selected_deploy()
    if dep:
      self._describe("deployment", dep, ns)


  def _deploy_delete(self):
    dep, ns = self._selected_deploy()
    if not dep:
      return
    if QMessageBox.question(self, "Delete Deployment",
                f'Delete deployment "{dep}"?\nThis will remove all its pods.',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
      self._run_cmd(f"kubectl delete deployment {dep} -n {ns} 2>&1",
             lambda o: (self._log(o), self._load_deployments()))

  # ── StatefulSets ──────────────────────────────────────────

  def _load_statefulsets(self):
    self._run_cmd(f"kubectl get statefulsets {self._ns_flag()} -o wide 2>&1",
           self._populate_statefulsets)


  def _populate_statefulsets(self, out: str):
    self.sts_list.clear()
    all_ns = (self._current_ns == "(all namespaces)")
    total = 0
    ready_count = 0
    for line in out.strip().splitlines()[1:]:
      parts = line.split()
      # `kubectl get statefulsets -o wide` columns: NAME READY AGE
      # CONTAINERS IMAGES (NAMESPACE prepended in --all-namespaces).
      if all_ns:
        if len(parts) < 4:
          continue
        ns, name, ready, age = parts[:4]
        imgs = " | ".join(parts[4:]) if len(parts) > 4 else "-"
      else:
        if len(parts) < 3:
          continue
        ns = self._current_ns or "default"
        name, ready, age = parts[:3]
        imgs = " | ".join(parts[3:]) if len(parts) > 3 else "-"
      meta = {
        "namespace": ns, "name": name, "ready": ready,
        "age": age, "images": imgs,
      }
      item = QListWidgetItem()
      item.setData(Qt.UserRole, meta)
      item.setSizeHint(QSize(0, StatefulSetCardWidget.CARD_HEIGHT))
      self.sts_list.addItem(item)
      self.sts_list.setItemWidget(item, StatefulSetCardWidget(meta, all_ns))
      total += 1
      try:
        cur, desired = ready.split("/")
        if cur == desired:
          ready_count += 1
      except Exception:
        pass
    if total == 0:
      color_key = "TEXT_MUTED"
    elif ready_count == total:
      color_key = "SUCCESS"
    elif ready_count == 0:
      color_key = "DANGER"
    else:
      color_key = "WARNING"
    self._set_count_badge(self.sts_count_lbl, f"{total} statefulset{'s' if total != 1 else ''} · {ready_count} ready", color_key)
    self._filter_statefulsets(self.sts_filter.text())


  def _on_sts_click(self, item):
    meta = item.data(Qt.UserRole) or {}
    try:
      _, desired = meta.get("ready", "").split("/")
      self.sts_scale_spin.setValue(int(desired))
    except Exception:
      pass


  def _on_sts_double_click(self, item):
    meta = item.data(Qt.UserRole) or {}
    self._describe("statefulset", meta.get("name"), meta.get("namespace") or "default")


  def _on_sts_selection_changed(self, current, previous):
    if previous is not None:
      w = self.sts_list.itemWidget(previous)
      if w:
        w.set_selected(False)
    if current is not None:
      w = self.sts_list.itemWidget(current)
      if w:
        w.set_selected(True)


  def _filter_statefulsets(self, text: str):
    q = text.lower()
    for i in range(self.sts_list.count()):
      item = self.sts_list.item(i)
      meta = item.data(Qt.UserRole) or {}
      item.setHidden(q not in meta.get("name", "").lower())


  def _selected_sts(self) -> tuple: # (Optional[str], str)
    item = self.sts_list.currentItem()
    if not item:
      QMessageBox.warning(self, "No selection", "Select a statefulset first.")
      return None, ""
    meta = item.data(Qt.UserRole) or {}
    return meta.get("name"), meta.get("namespace") or "default"


  def _sts_scale(self):
    sts, ns = self._selected_sts()
    if not sts:
      return
    replicas = self.sts_scale_spin.value()
    if QMessageBox.question(self, "Scale", f'Scale "{sts}" to {replicas} replica(s)?',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
      self._run_cmd(f"kubectl scale statefulset {sts} -n {ns} --replicas={replicas} 2>&1",
             lambda o: (self._log(o), self._load_statefulsets()))


  def _sts_restart(self):
    sts, ns = self._selected_sts()
    if sts:
      self._run_cmd(f"kubectl rollout restart statefulset/{sts} -n {ns} 2>&1",
             lambda o: (self._log(o), self._load_statefulsets()))


  def _sts_describe(self):
    sts, ns = self._selected_sts()
    if sts:
      self._describe("statefulset", sts, ns)


  def _sts_delete(self):
    sts, ns = self._selected_sts()
    if not sts:
      return
    if QMessageBox.question(self, "Delete StatefulSet",
                f'Delete statefulset "{sts}"?\nThis will remove all its pods.',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
      self._run_cmd(f"kubectl delete statefulset {sts} -n {ns} 2>&1",
             lambda o: (self._log(o), self._load_statefulsets()))


  def _sts_ctx_menu(self, pos):
    item = self.sts_list.itemAt(pos)
    if not item:
      return
    self.sts_list.setCurrentItem(item)
    meta = item.data(Qt.UserRole) or {}
    name = meta.get("name")
    ns  = meta.get("namespace") or "default"
    menu = QMenu(self)
    menu.addAction("↺ Restart", self._sts_restart)
    menu.addAction(" Describe", lambda: self._describe("statefulset", name, ns))
    menu.addSeparator()
    menu.addAction(" Delete", self._sts_delete)
    menu.exec_(self.sts_list.viewport().mapToGlobal(pos))

  # ── DaemonSets ────────────────────────────────────────────

  def _load_daemonsets(self):
    self._run_cmd(f"kubectl get daemonsets {self._ns_flag()} -o wide 2>&1",
           self._populate_daemonsets)


  def _populate_daemonsets(self, out: str):
    self.ds_list.clear()
    all_ns = (self._current_ns == "(all namespaces)")
    total = 0
    ready_count = 0
    for line in out.strip().splitlines()[1:]:
      parts = line.split()
      # `kubectl get daemonsets -o wide` columns: NAME DESIRED CURRENT
      # READY UP-TO-DATE AVAILABLE NODE-SELECTOR AGE CONTAINERS IMAGES
      # SELECTOR (NAMESPACE prepended in --all-namespaces). NODE-SELECTOR
      # renders as a single space-free token ("<none>" or a real
      # selector expression), so straight positional split() still
      # lines the fixed columns up correctly.
      if all_ns:
        if len(parts) < 9:
          continue
        ns, name, desired, current, ready, upd, avail, node_sel, age = parts[:9]
        imgs = " | ".join(parts[9:]) if len(parts) > 9 else "-"
      else:
        if len(parts) < 8:
          continue
        ns = self._current_ns or "default"
        name, desired, current, ready, upd, avail, node_sel, age = parts[:8]
        imgs = " | ".join(parts[8:]) if len(parts) > 8 else "-"
      meta = {
        "namespace": ns, "name": name, "desired": desired, "current": current,
        "ready": ready, "up_to_date": upd, "available": avail,
        "node_selector": node_sel, "age": age, "images": imgs,
      }
      item = QListWidgetItem()
      item.setData(Qt.UserRole, meta)
      item.setSizeHint(QSize(0, DaemonSetCardWidget.CARD_HEIGHT))
      self.ds_list.addItem(item)
      self.ds_list.setItemWidget(item, DaemonSetCardWidget(meta, all_ns))
      total += 1
      if desired == ready or desired == "0":
        ready_count += 1
    if total == 0:
      color_key = "TEXT_MUTED"
    elif ready_count == total:
      color_key = "SUCCESS"
    elif ready_count == 0:
      color_key = "DANGER"
    else:
      color_key = "WARNING"
    self._set_count_badge(self.ds_count_lbl, f"{total} daemonset{'s' if total != 1 else ''} · {ready_count} ready", color_key)
    self._filter_daemonsets(self.ds_filter.text())


  def _on_ds_double_click(self, item):
    meta = item.data(Qt.UserRole) or {}
    self._describe("daemonset", meta.get("name"), meta.get("namespace") or "default")


  def _on_ds_selection_changed(self, current, previous):
    if previous is not None:
      w = self.ds_list.itemWidget(previous)
      if w:
        w.set_selected(False)
    if current is not None:
      w = self.ds_list.itemWidget(current)
      if w:
        w.set_selected(True)


  def _filter_daemonsets(self, text: str):
    q = text.lower()
    for i in range(self.ds_list.count()):
      item = self.ds_list.item(i)
      meta = item.data(Qt.UserRole) or {}
      item.setHidden(q not in meta.get("name", "").lower())


  def _selected_ds(self) -> tuple: # (Optional[str], str)
    item = self.ds_list.currentItem()
    if not item:
      QMessageBox.warning(self, "No selection", "Select a daemonset first.")
      return None, ""
    meta = item.data(Qt.UserRole) or {}
    return meta.get("name"), meta.get("namespace") or "default"


  def _ds_restart(self):
    ds, ns = self._selected_ds()
    if ds:
      self._run_cmd(f"kubectl rollout restart daemonset/{ds} -n {ns} 2>&1",
             lambda o: (self._log(o), self._load_daemonsets()))


  def _ds_describe(self):
    ds, ns = self._selected_ds()
    if ds:
      self._describe("daemonset", ds, ns)


  def _ds_delete(self):
    ds, ns = self._selected_ds()
    if not ds:
      return
    if QMessageBox.question(self, "Delete DaemonSet",
                f'Delete daemonset "{ds}"?\nThis will remove it from every node.',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
      self._run_cmd(f"kubectl delete daemonset {ds} -n {ns} 2>&1",
             lambda o: (self._log(o), self._load_daemonsets()))


  def _ds_ctx_menu(self, pos):
    item = self.ds_list.itemAt(pos)
    if not item:
      return
    self.ds_list.setCurrentItem(item)
    meta = item.data(Qt.UserRole) or {}
    name = meta.get("name")
    ns  = meta.get("namespace") or "default"
    menu = QMenu(self)
    menu.addAction("↺ Restart", self._ds_restart)
    menu.addAction(" Describe", lambda: self._describe("daemonset", name, ns))
    menu.addSeparator()
    menu.addAction(" Delete", self._ds_delete)
    menu.exec_(self.ds_list.viewport().mapToGlobal(pos))

  # ── HorizontalPodAutoscalers ─────────────────────────────

  @staticmethod
  def _hpa_metric_value(d: dict) -> str:
    """Pull whichever value field a v2 HPA metric target/current dict
    actually carries — Resource/Pods/Object/External metrics all
    share this shape but populate different keys of it."""
    if not d:
      return "-"
    if "averageUtilization" in d:
      return f"{d['averageUtilization']}%"
    if "averageValue" in d:
      return str(d["averageValue"])
    if "value" in d:
      return str(d["value"])
    return "-"


  @classmethod
  def _hpa_metric_key_name(cls, m: dict, side: str):
    """(type_key, metric_name) for one metric entry — 'side' is
    'target' (from spec.metrics) or 'current' (from status.currentMetrics).
    Matching spec vs current entries by (type, name) is how a target %
    gets paired with its current % below."""
    mtype = m.get("type", "")
    key = mtype.lower()
    sub = m.get(key, {}) or {}
    name = (sub.get("metric") or {}).get("name") or sub.get("name") or mtype
    return (key, name), cls._hpa_metric_value(sub.get(side))


  def _load_hpas(self):
    self._run_cmd(f"kubectl get hpa {self._ns_flag()} -o json 2>&1", self._populate_hpas)


  def _populate_hpas(self, out: str):
    self.hpa_list.clear()
    all_ns = (self._current_ns == "(all namespaces)")
    try:
      items = json.loads(out).get("items", [])
    except Exception:
      items = []

    total = 0
    healthy_count = 0
    for it in items:
      spec  = it.get("spec") or {}
      status = it.get("status") or {}
      ref  = spec.get("scaleTargetRef") or {}

      metrics_spec  = spec.get("metrics") or []
      current_metrics = status.get("currentMetrics") or []

      # autoscaling/v1 HPAs (older clusters) don't have spec.metrics
      # at all — just a single implicit CPU-utilization target — so
      # synthesize the same one-entry shape the v2 path below expects.
      if not metrics_spec and spec.get("targetCPUUtilizationPercentage") is not None:
        metrics_spec = [{"type": "Resource", "resource": {
          "name": "cpu", "target": {"averageUtilization": spec["targetCPUUtilizationPercentage"]}}}]
        cur_cpu = status.get("currentCPUUtilizationPercentage")
        if cur_cpu is not None:
          current_metrics = [{"type": "Resource", "resource": {
            "name": "cpu", "current": {"averageUtilization": cur_cpu}}}]

      current_by_key = {}
      for m in current_metrics:
        key, val = self._hpa_metric_key_name(m, "current")
        current_by_key[key] = val

      metrics = []
      has_unknown = False
      for m in metrics_spec:
        key, target_val = self._hpa_metric_key_name(m, "target")
        current_val = current_by_key.get(key, "-")
        if current_val == "-":
          has_unknown = True
        metrics.append({"label": key[1], "current": current_val, "target": target_val})

      conditions = status.get("conditions") or []
      scaling_blocked = any(
        c.get("status") == "False" and c.get("type") in ("AbleToScale", "ScalingActive")
        for c in conditions
      )
      healthy = not has_unknown and not scaling_blocked

      meta = {
        "namespace":    (it.get("metadata") or {}).get("namespace", ""),
        "name":       (it.get("metadata") or {}).get("name", ""),
        "reference":    f"{ref.get('kind', '')}/{ref.get('name', '')}",
        "min_replicas":   spec.get("minReplicas", "-"),
        "max_replicas":   spec.get("maxReplicas", "-"),
        "current_replicas": status.get("currentReplicas", "-"),
        "desired_replicas": status.get("desiredReplicas", "-"),
        "metrics":     metrics,
        "age":       self._humanize_age((it.get("metadata") or {}).get("creationTimestamp", "")),
        "healthy":     healthy,
      }
      item = QListWidgetItem()
      item.setData(Qt.UserRole, meta)
      item.setSizeHint(QSize(0, HPACardWidget.CARD_HEIGHT))
      self.hpa_list.addItem(item)
      self.hpa_list.setItemWidget(item, HPACardWidget(meta, all_ns))
      total += 1
      if healthy:
        healthy_count += 1

    if total == 0:
      color_key = "TEXT_MUTED"
    elif healthy_count == total:
      color_key = "SUCCESS"
    elif healthy_count == 0:
      color_key = "DANGER"
    else:
      color_key = "WARNING"
    self._set_count_badge(self.hpa_count_lbl, f"{total} autoscaler{'s' if total != 1 else ''} · {healthy_count} healthy", color_key)
    self._filter_hpas(self.hpa_filter.text())


  def _filter_hpas(self, text: str):
    q = text.lower()
    for i in range(self.hpa_list.count()):
      item = self.hpa_list.item(i)
      meta = item.data(Qt.UserRole) or {}
      searchable = f"{meta.get('name', '')} {meta.get('reference', '')}".lower()
      item.setHidden(q not in searchable)


  def _on_hpa_selection_changed(self, current, previous):
    if previous is not None:
      w = self.hpa_list.itemWidget(previous)
      if w:
        w.set_selected(False)
    if current is not None:
      w = self.hpa_list.itemWidget(current)
      if w:
        w.set_selected(True)


  def _on_hpa_double_click(self, item):
    meta = item.data(Qt.UserRole) or {}
    self._describe("hpa", meta.get("name"), meta.get("namespace") or "default")


  def _selected_hpa(self) -> tuple: # (Optional[str], str)
    item = self.hpa_list.currentItem()
    if not item:
      QMessageBox.warning(self, "No selection", "Select an autoscaler first.")
      return None, ""
    meta = item.data(Qt.UserRole) or {}
    return meta.get("name"), meta.get("namespace") or "default"


  def _hpa_describe(self):
    name, ns = self._selected_hpa()
    if name:
      self._describe("hpa", name, ns)


  def _hpa_delete(self):
    name, ns = self._selected_hpa()
    if not name:
      return
    if QMessageBox.question(self, "Delete HPA",
                f'Delete autoscaler "{name}"?\nThe target workload keeps running at its '
                f'current replica count, but nothing will scale it automatically anymore.',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
      self._run_cmd(f"kubectl delete hpa {name} -n {ns} 2>&1",
             lambda o: (self._log(o), self._load_hpas()))


  def _hpa_ctx_menu(self, pos):
    item = self.hpa_list.itemAt(pos)
    if not item:
      return
    self.hpa_list.setCurrentItem(item)
    meta = item.data(Qt.UserRole) or {}
    name = meta.get("name")
    ns  = meta.get("namespace") or "default"
    menu = QMenu(self)
    menu.addAction(" Describe", lambda: self._describe("hpa", name, ns))
    menu.addSeparator()
    menu.addAction(" Delete", self._hpa_delete)
    menu.exec_(self.hpa_list.viewport().mapToGlobal(pos))

  # ── Services ──────────────────────────────────────────────

  def _load_services(self):
    self._run_cmd(f"kubectl get services {self._ns_flag()} 2>&1", self._populate_services)


  def _populate_services(self, out: str):
    self.svc_list.clear()
    all_ns = (self._current_ns == "(all namespaces)")
    total = 0
    for line in out.strip().splitlines()[1:]:
      parts = line.split()
      # `kubectl get services --all-namespaces` prepends NAMESPACE —
      # same shifted-columns reasoning as _populate_pods/_populate_deployments.
      if all_ns:
        if len(parts) < 7:
          continue
        ns, name, stype, cluster, ext, ports, age = parts[:7]
      else:
        if len(parts) < 6:
          continue
        ns = self._current_ns or "default"
        name, stype, cluster, ext, ports, age = parts[:6]
      meta = {
        "namespace": ns, "name": name, "type": stype,
        "cluster_ip": cluster, "external_ip": ext,
        "ports": ports, "age": age,
      }
      item = QListWidgetItem()
      item.setData(Qt.UserRole, meta)
      item.setSizeHint(QSize(0, ServiceCardWidget.CARD_HEIGHT))
      self.svc_list.addItem(item)
      self.svc_list.setItemWidget(item, ServiceCardWidget(meta, all_ns))
      total += 1
    color_key = "TEXT_MUTED" if total == 0 else "INFO"
    self._set_count_badge(self.svc_count_lbl,
                f"{total} service{'s' if total != 1 else ''}", color_key)
    # Same reasoning as _populate_pods: rebuild wipes the visual filter
    # state even though the filter box still has text — reapply it.
    self._filter_services(self.svc_filter.text())


  def _filter_services(self, text: str):
    q = text.lower()
    for i in range(self.svc_list.count()):
      item = self.svc_list.item(i)
      meta = item.data(Qt.UserRole) or {}
      item.setHidden(q not in meta.get("name", "").lower())


  def _selected_svc(self) -> tuple: # (Optional[str], str)
    item = self.svc_list.currentItem()
    if not item:
      QMessageBox.warning(self, "No selection", "Select a service first.")
      return None, ""
    meta = item.data(Qt.UserRole) or {}
    return meta.get("name"), meta.get("namespace") or "default"


  def _on_svc_double_click(self, item):
    meta = item.data(Qt.UserRole) or {}
    self._describe("service", meta.get("name"), meta.get("namespace") or "default")


  def _on_svc_selection_changed(self, current, previous):
    """Same reasoning as _on_pod_selection_changed above."""
    if previous is not None:
      w = self.svc_list.itemWidget(previous)
      if w:
        w.set_selected(False)
    if current is not None:
      w = self.svc_list.itemWidget(current)
      if w:
        w.set_selected(True)


  def _svc_describe(self):
    svc, ns = self._selected_svc()
    if svc:
      self._describe("service", svc, ns)


  def _svc_delete(self):
    svc, ns = self._selected_svc()
    if not svc:
      return
    if QMessageBox.question(self, "Delete Service", f'Delete service "{svc}"?',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
      self._run_cmd(f"kubectl delete service -n {ns} {svc} 2>&1",
             lambda o: (self._log(o), self._load_services()))


  def _svc_ctx_menu(self, pos):
    item = self.svc_list.itemAt(pos)
    if not item:
      return
    self.svc_list.setCurrentItem(item)
    meta = item.data(Qt.UserRole) or {}
    svc = meta.get("name")
    ns  = meta.get("namespace") or "default"
    menu = QMenu(self)
    menu.addAction(" Describe", lambda: self._describe("service", svc, ns))
    menu.addSeparator()
    menu.addAction(" Delete", self._svc_delete)
    menu.exec_(self.svc_list.viewport().mapToGlobal(pos))

  # ── Ingress ───────────────────────────────────────────────

  def _load_ingress(self):
    self._run_cmd(f"kubectl get ingress {self._ns_flag()} 2>&1", self._populate_ingress)


  def _populate_ingress(self, out: str):
    self.ing_list.clear()
    all_ns = (self._current_ns == "(all namespaces)")
    total = 0
    for line in out.strip().splitlines()[1:]:
      parts = line.split()
      if not parts:
        continue
      if all_ns:
        if len(parts) < 2:
          continue
        ns, name = parts[0], parts[1]
        rest = parts[2:]
      else:
        ns = self._current_ns or "default"
        name = parts[0]
        rest = parts[1:]
      cls   = rest[0] if len(rest) > 0 else "-"
      hosts  = rest[1] if len(rest) > 1 else "-"
      address = rest[2] if len(rest) > 2 else "-"
      ports  = rest[3] if len(rest) > 3 else "-"
      age   = rest[4] if len(rest) > 4 else "-"
      meta = {
        "namespace": ns, "name": name, "class": cls, "hosts": hosts,
        "address": address, "ports": ports, "age": age,
      }
      item = QListWidgetItem()
      item.setData(Qt.UserRole, meta)
      item.setSizeHint(QSize(0, IngressCardWidget.CARD_HEIGHT))
      self.ing_list.addItem(item)
      self.ing_list.setItemWidget(item, IngressCardWidget(meta, all_ns))
      total += 1
    color_key = "TEXT_MUTED" if total == 0 else "INFO"
    self._set_count_badge(self.ing_count_lbl,
                f"{total} rule{'s' if total != 1 else ''}", color_key)
    self._filter_ingress(self.ing_filter.text())


  def _filter_ingress(self, text: str):
    q = text.lower()
    for i in range(self.ing_list.count()):
      item = self.ing_list.item(i)
      meta = item.data(Qt.UserRole) or {}
      item.setHidden(q not in meta.get("name", "").lower())


  def _selected_ing(self) -> tuple: # (Optional[str], str)
    item = self.ing_list.currentItem()
    if not item:
      QMessageBox.warning(self, "No selection", "Select an ingress first.")
      return None, ""
    meta = item.data(Qt.UserRole) or {}
    return meta.get("name"), meta.get("namespace") or "default"


  def _on_ing_double_click(self, item):
    meta = item.data(Qt.UserRole) or {}
    self._describe("ingress", meta.get("name"), meta.get("namespace") or "default")


  def _on_ing_selection_changed(self, current, previous):
    """Same reasoning as _on_pod_selection_changed above."""
    if previous is not None:
      w = self.ing_list.itemWidget(previous)
      if w:
        w.set_selected(False)
    if current is not None:
      w = self.ing_list.itemWidget(current)
      if w:
        w.set_selected(True)


  def _ing_describe(self):
    ing, ns = self._selected_ing()
    if ing:
      self._describe("ingress", ing, ns)


  def _ing_delete(self):
    ing, ns = self._selected_ing()
    if not ing:
      return
    if QMessageBox.question(self, "Delete Ingress", f'Delete ingress "{ing}"?',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.Yes:
      self._run_cmd(f"kubectl delete ingress -n {ns} {ing} 2>&1",
             lambda o: (self._log(o), self._load_ingress()))


  def _ing_ctx_menu(self, pos):
    item = self.ing_list.itemAt(pos)
    if not item:
      return
    self.ing_list.setCurrentItem(item)
    meta = item.data(Qt.UserRole) or {}
    ing = meta.get("name")
    ns  = meta.get("namespace") or "default"
    menu = QMenu(self)
    menu.addAction(" Describe", lambda: self._describe("ingress", ing, ns))
    menu.addSeparator()
    menu.addAction(" Delete", self._ing_delete)
    menu.exec_(self.ing_list.viewport().mapToGlobal(pos))

  # ── Config & Secrets ──────────────────────────────────────

