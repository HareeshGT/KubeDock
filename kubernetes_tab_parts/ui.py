from .common import *


class KubernetesUiMixin:
  def _build_ui(self):
    root = QVBoxLayout(self)
    root.setContentsMargins(0, 0, 0, 0)
    root.setSpacing(0)

    # Control bar
    self.ctrl_bar = QWidget()
    self.ctrl_bar.setFixedHeight(52)
    self.ctrl_bar.setStyleSheet(
      f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
    )
    cb = QHBoxLayout(self.ctrl_bar)
    cb.setContentsMargins(12, 0, 12, 0)
    cb.setSpacing(10)

    # Kubernetes context picker — a jump host can have multiple clusters.
    self.context_group = QWidget()
    self.context_group.setObjectName("context_group")
    self.context_group.setFixedHeight(40)
    ctx_row = QHBoxLayout(self.context_group)
    ctx_row.setContentsMargins(14, 0, 8, 0)
    ctx_row.setSpacing(9)
    self.context_dot = QLabel("●")
    self.context_dot.setStyleSheet(f"color: {T['ACCENT2']}; font-size: 11px; background: transparent;")
    ctx_row.addWidget(self.context_dot)
    ctx_lbl = QLabel("CLUSTER")
    ctx_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 11px; font-weight: 700; letter-spacing: 0.5px; background: transparent;")
    ctx_row.addWidget(ctx_lbl)
    self.context_combo = QComboBox()
    self.context_combo.setObjectName("context_combo")
    self.context_combo.setMinimumWidth(190)
    self.context_combo.setFixedHeight(30)
    self.context_combo.setMaxVisibleItems(12)
    self.context_combo.setEditable(True)
    self.context_combo.setInsertPolicy(QComboBox.NoInsert)
    self.context_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
    self.context_combo.completer().setFilterMode(Qt.MatchContains)
    self.context_combo.currentTextChanged.connect(self._on_context_change)
    ctx_row.addWidget(self.context_combo)
    self._style_context_group()
    cb.addWidget(self.context_group)
    self.context_refresh_btn = self._toolbar_btn("↻", tooltip="Refresh Kubernetes contexts")
    self.context_refresh_btn.setFixedWidth(30)
    self.context_refresh_btn.setStyleSheet("padding: 0;")
    self.context_refresh_btn.clicked.connect(self._load_contexts)
    cb.addWidget(self.context_refresh_btn)

    cb.addWidget(self._vline())

    # Namespace picker, grouped into one rounded "chip" (dot + label +
    # combo sharing a pill background) instead of three bare widgets
    # floating loose on the toolbar — reads as a single catchy control
    # rather than a thin, easy-to-miss dropdown.
    self.ns_group = QWidget()
    self.ns_group.setObjectName("ns_group")
    self.ns_group.setFixedHeight(40)
    ns_row = QHBoxLayout(self.ns_group)
    ns_row.setContentsMargins(14, 0, 8, 0)
    ns_row.setSpacing(9)
    self.ns_dot = QLabel("●")
    self.ns_dot.setStyleSheet(f"color: {T['ACCENT']}; font-size: 11px; background: transparent;")
    ns_row.addWidget(self.ns_dot)
    ns_lbl = QLabel("NAMESPACE")
    ns_lbl.setStyleSheet(
      f"color: {T['TEXT_DIM']}; font-size: 11px; font-weight: 700; "
      f"letter-spacing: 0.5px; background: transparent;"
    )
    ns_row.addWidget(ns_lbl)
    self.ns_combo = QComboBox()
    self.ns_combo.setObjectName("ns_combo")
    self.ns_combo.setMinimumWidth(190)
    self.ns_combo.setFixedHeight(30)
    self.ns_combo.setMaxVisibleItems(12)
    self.ns_combo.setEditable(True)
    self.ns_combo.setInsertPolicy(QComboBox.NoInsert)
    self.ns_combo.completer().setCompletionMode(QCompleter.PopupCompletion)
    self.ns_combo.completer().setFilterMode(Qt.MatchContains)
    self.ns_combo.currentTextChanged.connect(self._on_ns_change)
    ns_row.addWidget(self.ns_combo)
    self._style_ns_group()
    cb.addWidget(self.ns_group)

    # Namespace create — small icon button living right next to the
    # picker rather than buried in a menu, since switching is already
    # the picker's job.
    self.ns_new_btn = self._toolbar_btn("＋", tooltip="Create namespace…")
    self.ns_new_btn.setFixedWidth(30)
    self.ns_new_btn.setStyleSheet("padding: 0;")
    self.ns_new_btn.clicked.connect(self._create_namespace)
    cb.addWidget(self.ns_new_btn)

    cb.addWidget(self._vline())

    self.refresh_btn = self._toolbar_btn(" Refresh")
    self.refresh_btn.clicked.connect(self._refresh_current_tab)
    cb.addWidget(self.refresh_btn)

    self.auto_btn = self._toolbar_btn("⏱ Auto (30 s)")
    self.auto_btn.setCheckable(True)
    self.auto_btn.toggled.connect(self._toggle_auto_refresh)
    cb.addWidget(self.auto_btn)
    cb.addWidget(self._vline())

    self.health_lbl = QLabel("● Cluster")
    self.health_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
    cb.addWidget(self.health_lbl)
    cb.addStretch()

    # self.kubectl_inp = QLineEdit()
    # self.kubectl_inp.setPlaceholderText("kubectl … (raw command)")
    # self.kubectl_inp.setMaximumWidth(320)
    # self.kubectl_inp.returnPressed.connect(self._run_kubectl)
    # cb.addWidget(self.kubectl_inp)

    # self.run_btn = self._toolbar_btn("Run")
    # self.run_btn.clicked.connect(self._run_kubectl)
    # cb.addWidget(self.run_btn)
    # root.addWidget(self.ctrl_bar)
    root.addWidget(self.ctrl_bar)

    self.progress = QProgressBar()
    self.progress.setFixedHeight(3)
    self.progress.setRange(0, 0)
    self.progress.hide()
    root.addWidget(self.progress)

    self.sub_tabs = QTabWidget()
    self.sub_tabs.setTabPosition(QTabWidget.North)
    self.sub_tabs.currentChanged.connect(self._refresh_current_tab)
    root.addWidget(self.sub_tabs)

    self._build_pods_tab()
    self._build_deployments_tab()
    self._build_statefulsets_tab()
    self._build_daemonsets_tab()
    self._build_hpa_tab()
    self._build_services_tab()
    self._build_ingress_tab()
    self._build_jobs_tab()
    self._build_storage_tab()
    self._build_config_tab()
    self._build_events_tab()
    self._build_tunnels_tab()
    self._build_terminal_tab()
    self._build_ai_ops_tab()


  def _build_pods_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    tb = QHBoxLayout()
    tb.setContentsMargins(10, 6, 10, 6)
    tb.setSpacing(8)
    self.pod_filter = QLineEdit()
    self.pod_filter.setPlaceholderText(" Filter pods…")
    self.pod_filter.setMaximumWidth(200)
    self.pod_filter.textChanged.connect(self._filter_pods)
    tb.addWidget(self.pod_filter)

    self.pod_count_lbl = QLabel("")
    tb.addWidget(self.pod_count_lbl)
    tb.addStretch()

    # Safe, frequent actions live inside one clustered pill; the
    # destructive action (Delete) sits outside it with a gap, so it
    # can never be misclicked as "just another button in the row".
    cluster = QFrame()
    cluster.setObjectName("action_cluster")
    cl = QHBoxLayout(cluster)
    cl.setContentsMargins(4, 4, 4, 4)
    cl.setSpacing(2)
    for label, obj, slot in [
      (" Logs",  "pod_logs_btn",  self._pod_logs),
      (" Exec",  "pod_exec_btn",  self._pod_exec),
      ("↺ Restart", "pod_restart_btn", self._pod_restart),
    ]:
      btn = self._toolbar_btn(label)
      btn.setFlat(True)
      btn.setStyleSheet("border: none; padding: 0 14px; background: transparent;")
      setattr(self, obj, btn)
      btn.clicked.connect(slot)
      cl.addWidget(btn)
    tb.addWidget(cluster)
    self.pod_action_cluster = cluster

    self.pod_del_btn = self._toolbar_btn(" Delete", object_name="danger")
    self.pod_del_btn.clicked.connect(self._pod_delete)
    tb.addWidget(self.pod_del_btn)

    cluster.setStyleSheet(
      f"QFrame#action_cluster {{ background: {T['BG_ITEM']}; border-radius: 8px; }}"
    )

    tb_widget = QWidget()
    tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
    tb_widget.setLayout(tb)
    lay.addWidget(tb_widget)
    self.pods_toolbar = tb_widget

    # Pods render as cards (see k8s_cards.py) rather than table rows —
    # each card carries its own meta dict via Qt.UserRole, the same
    # "meta dict + setItemWidget()" pattern file_widgets.py already uses
    # for the file list. Namespace is folded into a chip on the card
    # itself (only shown in "(all namespaces)" view) instead of a
    # dedicated hidden column.
    self.pod_list = QListWidget()
    self.pod_list.setSpacing(6)
    self.pod_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.pod_list.setContextMenuPolicy(Qt.CustomContextMenu)
    self.pod_list.customContextMenuRequested.connect(self._pod_ctx_menu)
    self.pod_list.itemDoubleClicked.connect(self._on_pod_double_click)
    self.pod_list.currentItemChanged.connect(self._on_pod_selection_changed)
    self.pod_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 8px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    lay.addWidget(self.pod_list)
    add_icon_tab(self.sub_tabs, w, " Pods")


  def _build_deployments_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    tb = QHBoxLayout()
    tb.setContentsMargins(10, 6, 10, 6)
    tb.setSpacing(8)
    self.deploy_filter = QLineEdit()
    self.deploy_filter.setPlaceholderText(" Filter deployments…")
    self.deploy_filter.setMaximumWidth(200)
    self.deploy_filter.textChanged.connect(self._filter_deployments)
    tb.addWidget(self.deploy_filter)

    self.deploy_count_lbl = QLabel("")
    tb.addWidget(self.deploy_count_lbl)
    tb.addStretch()

    self.scale_spin = QSpinBox()
    self.scale_spin.setRange(0, 100)
    self.scale_spin.setValue(1)
    self.scale_spin.setFixedWidth(70)
    self.scale_spin.setToolTip("Replicas")
    tb.addWidget(QLabel("Replicas:"))

    self.scale_minus_btn = self._toolbar_btn(
      "−", tooltip="Scale down by 1 replica (applies immediately)")
    self.scale_minus_btn.setFixedWidth(32)
    # _toolbar_btn's shared style adds 14px of padding on each side,
    # which at this button's width left almost nothing for the glyph
    # itself — override it so "−"/"+" actually render, centered.
    self.scale_minus_btn.setStyleSheet("padding: 0; font-size: 16px; font-weight: 600;")
    self.scale_minus_btn.clicked.connect(lambda: self._deploy_scale_step(-1))
    tb.addWidget(self.scale_minus_btn)

    tb.addWidget(self.scale_spin)

    self.scale_plus_btn = self._toolbar_btn(
      "+", tooltip="Scale up by 1 replica (applies immediately)")
    self.scale_plus_btn.setFixedWidth(32)
    self.scale_plus_btn.setStyleSheet("padding: 0; font-size: 16px; font-weight: 600;")
    self.scale_plus_btn.clicked.connect(lambda: self._deploy_scale_step(1))
    tb.addWidget(self.scale_plus_btn)

    for label, obj, slot in [
      ("⇅ Scale",  "dep_scale_btn",  self._deploy_scale),
      ("↺ Restart", "dep_restart_btn", self._deploy_restart),
      (" Describe", "dep_desc_btn",  self._deploy_describe),
      (" Delete",  "dep_del_btn",   self._deploy_delete),
    ]:
      obj_name = "danger" if "Delete" in label else ("primary" if "" in label else None)
      btn = self._toolbar_btn(label, object_name=obj_name)
      setattr(self, obj, btn)
      btn.clicked.connect(slot)
      tb.addWidget(btn)

    tb_widget = QWidget()
    tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
    tb_widget.setLayout(tb)
    lay.addWidget(tb_widget)
    self.deploy_toolbar = tb_widget

    # Same card-list treatment as Pods — see k8s_cards.py.
    self.deploy_list = QListWidget()
    self.deploy_list.setSpacing(6)
    self.deploy_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.deploy_list.itemClicked.connect(self._on_deploy_click)
    self.deploy_list.itemDoubleClicked.connect(self._on_deploy_double_click)
    self.deploy_list.currentItemChanged.connect(self._on_deploy_selection_changed)
    self.deploy_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 8px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    lay.addWidget(self.deploy_list)
    add_icon_tab(self.sub_tabs, w, " Deployments")


  def _build_statefulsets_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    tb = QHBoxLayout()
    tb.setContentsMargins(10, 6, 10, 6)
    tb.setSpacing(8)
    self.sts_filter = QLineEdit()
    self.sts_filter.setPlaceholderText(" Filter statefulsets…")
    self.sts_filter.setMaximumWidth(200)
    self.sts_filter.textChanged.connect(self._filter_statefulsets)
    tb.addWidget(self.sts_filter)

    self.sts_count_lbl = QLabel("")
    tb.addWidget(self.sts_count_lbl)
    tb.addStretch()

    self.sts_scale_spin = QSpinBox()
    self.sts_scale_spin.setRange(0, 100)
    self.sts_scale_spin.setValue(1)
    self.sts_scale_spin.setFixedWidth(70)
    self.sts_scale_spin.setToolTip("Replicas")
    tb.addWidget(QLabel("Replicas:"))
    tb.addWidget(self.sts_scale_spin)

    for label, obj, slot in [
      ("⇅ Scale",   "sts_scale_btn",  self._sts_scale),
      ("↺ Restart",  "sts_restart_btn", self._sts_restart),
      (" Describe", "sts_desc_btn",  self._sts_describe),
      (" Delete",  "sts_del_btn",   self._sts_delete),
    ]:
      obj_name = "danger" if "Delete" in label else None
      btn = self._toolbar_btn(label, object_name=obj_name)
      setattr(self, obj, btn)
      btn.clicked.connect(slot)
      tb.addWidget(btn)

    tb_widget = QWidget()
    tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
    tb_widget.setLayout(tb)
    lay.addWidget(tb_widget)
    self.sts_toolbar = tb_widget

    self.sts_list = QListWidget()
    self.sts_list.setSpacing(6)
    self.sts_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.sts_list.setContextMenuPolicy(Qt.CustomContextMenu)
    self.sts_list.customContextMenuRequested.connect(self._sts_ctx_menu)
    self.sts_list.itemClicked.connect(self._on_sts_click)
    self.sts_list.itemDoubleClicked.connect(self._on_sts_double_click)
    self.sts_list.currentItemChanged.connect(self._on_sts_selection_changed)
    self.sts_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 8px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    lay.addWidget(self.sts_list)
    add_icon_tab(self.sub_tabs, w, " StatefulSets")


  def _build_daemonsets_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    tb = QHBoxLayout()
    tb.setContentsMargins(10, 6, 10, 6)
    tb.setSpacing(8)
    self.ds_filter = QLineEdit()
    self.ds_filter.setPlaceholderText(" Filter daemonsets…")
    self.ds_filter.setMaximumWidth(200)
    self.ds_filter.textChanged.connect(self._filter_daemonsets)
    tb.addWidget(self.ds_filter)

    self.ds_count_lbl = QLabel("")
    tb.addWidget(self.ds_count_lbl)
    tb.addStretch()

    # No Scale control — DaemonSets run exactly one pod per matching
    # node, so "replica count" isn't a thing a person can set here.
    for label, obj, slot in [
      ("↺ Restart",  "ds_restart_btn", self._ds_restart),
      (" Describe", "ds_desc_btn",  self._ds_describe),
      (" Delete",  "ds_del_btn",   self._ds_delete),
    ]:
      obj_name = "danger" if "Delete" in label else None
      btn = self._toolbar_btn(label, object_name=obj_name)
      setattr(self, obj, btn)
      btn.clicked.connect(slot)
      tb.addWidget(btn)

    tb_widget = QWidget()
    tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
    tb_widget.setLayout(tb)
    lay.addWidget(tb_widget)
    self.ds_toolbar = tb_widget

    self.ds_list = QListWidget()
    self.ds_list.setSpacing(6)
    self.ds_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.ds_list.setContextMenuPolicy(Qt.CustomContextMenu)
    self.ds_list.customContextMenuRequested.connect(self._ds_ctx_menu)
    self.ds_list.itemDoubleClicked.connect(self._on_ds_double_click)
    self.ds_list.currentItemChanged.connect(self._on_ds_selection_changed)
    self.ds_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 8px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    lay.addWidget(self.ds_list)
    add_icon_tab(self.sub_tabs, w, " DaemonSets")


  def _build_hpa_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    tb = QHBoxLayout()
    tb.setContentsMargins(10, 6, 10, 6)
    tb.setSpacing(8)
    self.hpa_filter = QLineEdit()
    self.hpa_filter.setPlaceholderText(" Filter autoscalers…")
    self.hpa_filter.setMaximumWidth(200)
    self.hpa_filter.textChanged.connect(self._filter_hpas)
    tb.addWidget(self.hpa_filter)

    self.hpa_count_lbl = QLabel("")
    tb.addWidget(self.hpa_count_lbl)
    tb.addStretch()

    # Read-only status view — no Scale button, since an HPA's whole
    # point is that it decides replica counts itself. Describe/Delete
    # are still useful (Delete to hand control back to a manual
    # `kubectl scale`, Describe to see the full condition history
    # behind why it hasn't scaled).
    self.hpa_desc_btn = self._toolbar_btn(" Describe")
    self.hpa_desc_btn.clicked.connect(self._hpa_describe)
    tb.addWidget(self.hpa_desc_btn)

    self.hpa_del_btn = self._toolbar_btn(" Delete", object_name="danger")
    self.hpa_del_btn.clicked.connect(self._hpa_delete)
    tb.addWidget(self.hpa_del_btn)

    tb_widget = QWidget()
    tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
    tb_widget.setLayout(tb)
    lay.addWidget(tb_widget)
    self.hpa_toolbar = tb_widget

    self.hpa_list = QListWidget()
    self.hpa_list.setSpacing(6)
    self.hpa_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.hpa_list.setContextMenuPolicy(Qt.CustomContextMenu)
    self.hpa_list.customContextMenuRequested.connect(self._hpa_ctx_menu)
    self.hpa_list.itemDoubleClicked.connect(self._on_hpa_double_click)
    self.hpa_list.currentItemChanged.connect(self._on_hpa_selection_changed)
    self.hpa_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 8px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    lay.addWidget(self.hpa_list)
    add_icon_tab(self.sub_tabs, w, " HPA")


  def _build_services_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    tb = QHBoxLayout()
    tb.setContentsMargins(10, 6, 10, 6)
    tb.setSpacing(8)
    self.svc_filter = QLineEdit()
    self.svc_filter.setPlaceholderText(" Filter services…")
    self.svc_filter.setMaximumWidth(200)
    self.svc_filter.textChanged.connect(self._filter_services)
    tb.addWidget(self.svc_filter)

    self.svc_count_lbl = QLabel("")
    tb.addWidget(self.svc_count_lbl)
    tb.addStretch()

    self.svc_desc_btn = self._toolbar_btn(" Describe")
    self.svc_desc_btn.clicked.connect(self._svc_describe)
    tb.addWidget(self.svc_desc_btn)

    self.svc_del_btn = self._toolbar_btn(" Delete", object_name="danger")
    self.svc_del_btn.clicked.connect(self._svc_delete)
    tb.addWidget(self.svc_del_btn)

    tb_widget = QWidget()
    tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
    tb_widget.setLayout(tb)
    lay.addWidget(tb_widget)
    self.svc_toolbar = tb_widget

    # Same card-list treatment as Pods/Deployments — see k8s_cards.py.
    self.svc_list = QListWidget()
    self.svc_list.setSpacing(6)
    self.svc_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.svc_list.setContextMenuPolicy(Qt.CustomContextMenu)
    self.svc_list.customContextMenuRequested.connect(self._svc_ctx_menu)
    self.svc_list.itemDoubleClicked.connect(self._on_svc_double_click)
    self.svc_list.currentItemChanged.connect(self._on_svc_selection_changed)
    self.svc_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 8px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    lay.addWidget(self.svc_list)
    add_icon_tab(self.sub_tabs, w, " Services")


  def _build_ingress_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    tb = QHBoxLayout()
    tb.setContentsMargins(10, 6, 10, 6)
    tb.setSpacing(8)
    self.ing_filter = QLineEdit()
    self.ing_filter.setPlaceholderText(" Filter ingress…")
    self.ing_filter.setMaximumWidth(200)
    self.ing_filter.textChanged.connect(self._filter_ingress)
    tb.addWidget(self.ing_filter)

    self.ing_count_lbl = QLabel("")
    tb.addWidget(self.ing_count_lbl)
    tb.addStretch()

    self.ing_desc_btn = self._toolbar_btn(" Describe")
    self.ing_desc_btn.clicked.connect(self._ing_describe)
    tb.addWidget(self.ing_desc_btn)

    self.ing_del_btn = self._toolbar_btn(" Delete", object_name="danger")
    self.ing_del_btn.clicked.connect(self._ing_delete)
    tb.addWidget(self.ing_del_btn)

    tb_widget = QWidget()
    tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
    tb_widget.setLayout(tb)
    lay.addWidget(tb_widget)
    self.ing_toolbar = tb_widget

    self.ing_list = QListWidget()
    self.ing_list.setSpacing(6)
    self.ing_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.ing_list.setContextMenuPolicy(Qt.CustomContextMenu)
    self.ing_list.customContextMenuRequested.connect(self._ing_ctx_menu)
    self.ing_list.itemDoubleClicked.connect(self._on_ing_double_click)
    self.ing_list.currentItemChanged.connect(self._on_ing_selection_changed)
    self.ing_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 8px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    lay.addWidget(self.ing_list)
    add_icon_tab(self.sub_tabs, w, " Ingress")



  def _build_ai_ops_tab(self):
    """Natural-language Kubernetes operations powered by AI."""

    w = QWidget()

    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    self.k8s_ai_ops = K8sAIOpsWidget(
      kube_context_getter=lambda: self._current_context,
      ssh=self.ssh,
      namespace_getter=lambda: self._current_ns,
      context_getter=self._ai_ops_context,
      parent=w,
    )

    self.k8s_ai_ops.operation_finished.connect(
      self._on_ai_ops_operation_finished
    )

    lay.addWidget(self.k8s_ai_ops)

    add_icon_tab(self.sub_tabs, w, "Ops Mind")


  def _ai_ops_context(self):
    """Give Ops Mind a small amount of useful UI context.

    The AI still has to identify the resource explicitly unless the user
    gives enough information. This context is only there to improve
    interpretation.
    """

    parts = []

    # Current namespace.
    parts.append(
      f"Selected namespace: {self._current_ns or 'default'}"
    )

    # Selected deployment, if any.
    try:
      item = self.deploy_list.currentItem()

      if item is not None:
        meta = item.data(Qt.UserRole) or {}

        name = meta.get("name")
        namespace = meta.get("namespace")

        if name:
          parts.append(
            f"Selected deployment: {name}"
          )

        if namespace:
          parts.append(
            f"Selected deployment namespace: {namespace}"
          )
    except Exception:
      pass

    # Selected pod, if any.
    try:
      item = self.pod_list.currentItem()

      if item is not None:
        meta = item.data(Qt.UserRole) or {}

        name = meta.get("name")
        namespace = meta.get("namespace")

        if name:
          parts.append(
            f"Selected pod: {name}"
          )

        if namespace:
          parts.append(
            f"Selected pod namespace: {namespace}"
          )
    except Exception:
      pass

    return "\n".join(parts)


  def _on_ai_ops_operation_finished(self):
    """Refresh the visible Kubernetes resource list after an AI operation."""

    try:
      self._refresh_current_tab()
    except Exception:
      pass

  # ── Jobs & CronJobs ───────────────────────────────────────

  def _build_jobs_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    splitter = QSplitter(Qt.Horizontal)
    splitter.setHandleWidth(1)

    left = QWidget()
    ll = QVBoxLayout(left)
    ll.setContentsMargins(0, 0, 0, 0)
    ll.setSpacing(0)

    self.wl_type_bar = QWidget()
    self.wl_type_bar.setFixedHeight(58)
    self.wl_type_bar.setStyleSheet(
      f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
    )
    tb_lay = QHBoxLayout(self.wl_type_bar)
    tb_lay.setContentsMargins(12, 10, 12, 10)
    tb_lay.setSpacing(10)

    self.wl_type_toggle = QWidget()
    self.wl_type_toggle.setObjectName("wl_type_toggle")
    self.wl_type_toggle.setFixedHeight(36)
    toggle_lay = QHBoxLayout(self.wl_type_toggle)
    toggle_lay.setContentsMargins(3, 3, 3, 3)
    toggle_lay.setSpacing(2)
    self.wl_type_jobs_btn = icon_button(" Jobs")
    self.wl_type_cron_btn = QPushButton("⏰ CronJobs")
    for btn in (self.wl_type_jobs_btn, self.wl_type_cron_btn):
      btn.setCheckable(True)
      btn.setCursor(Qt.PointingHandCursor)
      btn.setFixedHeight(30)
      toggle_lay.addWidget(btn)
    self.wl_type_jobs_btn.setChecked(True)
    self.wl_type_jobs_btn.clicked.connect(lambda: self._set_workload_type("Jobs"))
    self.wl_type_cron_btn.clicked.connect(lambda: self._set_workload_type("CronJobs"))
    self._workload_type = "Jobs"
    self._style_toggle(self.wl_type_toggle, (self.wl_type_jobs_btn, self.wl_type_cron_btn))
    tb_lay.addWidget(self.wl_type_toggle)

    self.wl_filter = QLineEdit()
    self.wl_filter.setPlaceholderText(" Filter…")
    self.wl_filter.textChanged.connect(self._filter_workloads)
    tb_lay.addWidget(self.wl_filter, 1)

    self.wl_count_lbl = QLabel("")
    tb_lay.addWidget(self.wl_count_lbl)
    ll.addWidget(self.wl_type_bar)

    wl_actions = QHBoxLayout()
    wl_actions.setContentsMargins(10, 6, 10, 6)
    wl_actions.setSpacing(8)

    self.wl_trigger_btn = self._toolbar_btn("▶ Trigger Now", object_name="primary",
                         tooltip="Manually run this CronJob now")
    self.wl_trigger_btn.clicked.connect(self._workload_trigger_now)
    wl_actions.addWidget(self.wl_trigger_btn)

    self.wl_suspend_btn = self._toolbar_btn("⏸ Suspend",
                         tooltip="Toggle Suspend/Resume for this CronJob")
    self.wl_suspend_btn.clicked.connect(self._workload_toggle_suspend)
    wl_actions.addWidget(self.wl_suspend_btn)

    wl_actions.addStretch()

    self.wl_desc_btn = self._toolbar_btn(" Describe")
    self.wl_desc_btn.clicked.connect(self._workload_describe)
    wl_actions.addWidget(self.wl_desc_btn)

    self.wl_del_btn = self._toolbar_btn(" Delete", object_name="danger")
    self.wl_del_btn.clicked.connect(self._workload_delete)
    wl_actions.addWidget(self.wl_del_btn)

    wl_actions_widget = QWidget()
    wl_actions_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
    wl_actions_widget.setLayout(wl_actions)
    ll.addWidget(wl_actions_widget)
    self.wl_toolbar = wl_actions_widget

    self.wl_list = QListWidget()
    self.wl_list.setSpacing(6)
    self.wl_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.wl_list.setContextMenuPolicy(Qt.CustomContextMenu)
    self.wl_list.customContextMenuRequested.connect(self._workload_ctx_menu)
    self.wl_list.itemDoubleClicked.connect(self._on_workload_double_click)
    self.wl_list.currentItemChanged.connect(self._on_workload_selection_changed)
    self.wl_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 8px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    ll.addWidget(self.wl_list)
    splitter.addWidget(left)

    right = QWidget()
    rl = QVBoxLayout(right)
    rl.setContentsMargins(0, 0, 0, 0)
    rl.setSpacing(0)

    self.wl_history_hdr = QLabel(" Run History")
    self.wl_history_hdr.setFixedHeight(34)
    self.wl_history_hdr.setStyleSheet(
      f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
      f"font-weight: 700; border-bottom: 1px solid {T['BORDER']}; padding-left: 14px;"
    )
    rl.addWidget(self.wl_history_hdr)

    self.wl_history_hint = QLabel(" Select a CronJob to see its recent Job runs.")
    self.wl_history_hint.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px; padding: 12px;")
    self.wl_history_hint.setWordWrap(True)
    rl.addWidget(self.wl_history_hint)

    self.wl_history_list = QListWidget()
    self.wl_history_list.setSpacing(6)
    self.wl_history_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.wl_history_list.itemDoubleClicked.connect(self._on_wl_history_double_click)
    self.wl_history_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 8px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    rl.addWidget(self.wl_history_list)

    splitter.addWidget(right)
    splitter.setSizes([440, 480])
    lay.addWidget(splitter)
    add_icon_tab(self.sub_tabs, w, "Jobs && CronJobs")
    self._update_workload_action_visibility()


  def _style_toggle(self, widget, buttons):
    """Generic segmented-toggle styling shared by every ConfigMaps/
    Secrets-style two-way switch on this tab (Config's own toggle
    keeps its dedicated _style_cfg_toggle since it existed first —
    this is for the newer Storage/Jobs toggles so their CSS doesn't
    have to be copy-pasted per tab). Re-called from apply_theme()."""
    widget.setStyleSheet(
      f"QWidget#{widget.objectName()} {{ background: {T['BG_ITEM']}; "
      f"border: 1px solid {T['BORDER']}; border-radius: 18px; }}"
    )
    btn_css = f"""
      QPushButton {{
        background: transparent; color: {T['TEXT_DIM']};
        border: none; border-radius: 15px; padding: 0 16px;
        font-size: 12px; font-weight: 700;
      }}
      QPushButton:hover:!checked {{ background: {T['BG_HOVER']}; color: {T['TEXT_PRIMARY']}; }}
      QPushButton:checked {{ background: {T['ACCENT']}; color: white; }}
    """
    for b in buttons:
      b.setStyleSheet(btn_css)


  def _update_workload_action_visibility(self):
    """Trigger Now / Suspend only make sense for CronJobs — Jobs are
    one-shot and have no schedule to suspend."""
    is_cron = (self._workload_type == "CronJobs")
    self.wl_trigger_btn.setVisible(is_cron)
    self.wl_suspend_btn.setVisible(is_cron)
    self.wl_history_hdr.setVisible(is_cron)
    self.wl_history_hint.setVisible(is_cron)
    self.wl_history_list.setVisible(is_cron)
    if not is_cron:
      self.wl_history_list.clear()


  def _set_workload_type(self, name: str):
    self._workload_type = name
    self.wl_type_jobs_btn.setChecked(name == "Jobs")
    self.wl_type_cron_btn.setChecked(name == "CronJobs")
    self._update_workload_action_visibility()
    self._load_workloads()


  def _build_storage_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    self.pvx_type_bar = QWidget()
    self.pvx_type_bar.setFixedHeight(58)
    self.pvx_type_bar.setStyleSheet(
      f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
    )
    tb_lay = QHBoxLayout(self.pvx_type_bar)
    tb_lay.setContentsMargins(12, 10, 12, 10)
    tb_lay.setSpacing(10)

    self.pvx_type_toggle = QWidget()
    self.pvx_type_toggle.setObjectName("pvx_type_toggle")
    self.pvx_type_toggle.setFixedHeight(36)
    toggle_lay = QHBoxLayout(self.pvx_type_toggle)
    toggle_lay.setContentsMargins(3, 3, 3, 3)
    toggle_lay.setSpacing(2)
    self.pvx_type_pvc_btn = icon_button(" Claims (PVC)")
    self.pvx_type_pv_btn = icon_button(" Volumes (PV)")
    for btn in (self.pvx_type_pvc_btn, self.pvx_type_pv_btn):
      btn.setCheckable(True)
      btn.setCursor(Qt.PointingHandCursor)
      btn.setFixedHeight(30)
      toggle_lay.addWidget(btn)
    self.pvx_type_pvc_btn.setChecked(True)
    self.pvx_type_pvc_btn.clicked.connect(lambda: self._set_storage_type("PVC"))
    self.pvx_type_pv_btn.clicked.connect(lambda: self._set_storage_type("PV"))
    self._storage_type = "PVC"
    self._style_toggle(self.pvx_type_toggle, (self.pvx_type_pvc_btn, self.pvx_type_pv_btn))
    tb_lay.addWidget(self.pvx_type_toggle)

    self.pvx_filter = QLineEdit()
    self.pvx_filter.setPlaceholderText(" Filter…")
    self.pvx_filter.textChanged.connect(self._filter_storage)
    tb_lay.addWidget(self.pvx_filter, 1)

    self.pvx_count_lbl = QLabel("")
    tb_lay.addWidget(self.pvx_count_lbl)
    lay.addWidget(self.pvx_type_bar)

    pvx_actions = QHBoxLayout()
    pvx_actions.setContentsMargins(10, 6, 10, 6)
    pvx_actions.setSpacing(8)
    pvx_actions.addStretch()

    self.pvx_desc_btn = self._toolbar_btn(" Describe")
    self.pvx_desc_btn.clicked.connect(self._storage_describe)
    pvx_actions.addWidget(self.pvx_desc_btn)

    self.pvx_del_btn = self._toolbar_btn(" Delete", object_name="danger")
    self.pvx_del_btn.clicked.connect(self._storage_delete)
    pvx_actions.addWidget(self.pvx_del_btn)

    pvx_actions_widget = QWidget()
    pvx_actions_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
    pvx_actions_widget.setLayout(pvx_actions)
    lay.addWidget(pvx_actions_widget)
    self.pvx_toolbar = pvx_actions_widget

    self.pvx_list = QListWidget()
    self.pvx_list.setSpacing(6)
    self.pvx_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.pvx_list.setContextMenuPolicy(Qt.CustomContextMenu)
    self.pvx_list.customContextMenuRequested.connect(self._storage_ctx_menu)
    self.pvx_list.itemDoubleClicked.connect(self._on_storage_double_click)
    self.pvx_list.currentItemChanged.connect(self._on_storage_selection_changed)
    self.pvx_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 8px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    lay.addWidget(self.pvx_list)
    add_icon_tab(self.sub_tabs, w, " Storage")


  def _set_storage_type(self, name: str):
    self._storage_type = name
    self.pvx_type_pvc_btn.setChecked(name == "PVC")
    self.pvx_type_pv_btn.setChecked(name == "PV")
    self._load_storage()


  def _build_config_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    splitter = QSplitter(Qt.Horizontal)
    splitter.setHandleWidth(1)

    # Left: type toggle + list
    left = QWidget()
    ll = QVBoxLayout(left)
    ll.setContentsMargins(0, 0, 0, 0)
    ll.setSpacing(0)

    # Taller toolbar with real breathing room — the old 42px bar packed
    # a combo box and filter field edge-to-edge with almost no margin,
    # which is most of what read as "congested".
    self.cfg_type_bar = QWidget()
    self.cfg_type_bar.setFixedHeight(58)
    self.cfg_type_bar.setStyleSheet(
      f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};"
    )
    tb_lay = QHBoxLayout(self.cfg_type_bar)
    tb_lay.setContentsMargins(12, 10, 12, 10)
    tb_lay.setSpacing(10)

    # ConfigMaps/Secrets is a binary choice, not a long list — a
    # segmented two-button toggle reads faster than opening a dropdown
    # for one of two options, and gives the "big, catchy" control the
    # namespace picker also got, instead of a thin QComboBox.
    self.cfg_type_toggle = QWidget()
    self.cfg_type_toggle.setObjectName("cfg_type_toggle")
    self.cfg_type_toggle.setFixedHeight(36)
    toggle_lay = QHBoxLayout(self.cfg_type_toggle)
    toggle_lay.setContentsMargins(3, 3, 3, 3)
    toggle_lay.setSpacing(2)
    self.cfg_type_cm_btn = icon_button(" ConfigMaps")
    self.cfg_type_secret_btn = icon_button(" Secrets")
    for btn in (self.cfg_type_cm_btn, self.cfg_type_secret_btn):
      btn.setCheckable(True)
      btn.setCursor(Qt.PointingHandCursor)
      btn.setFixedHeight(30)
      toggle_lay.addWidget(btn)
    self.cfg_type_cm_btn.setChecked(True)
    self.cfg_type_cm_btn.clicked.connect(lambda: self._set_cfg_type("ConfigMaps"))
    self.cfg_type_secret_btn.clicked.connect(lambda: self._set_cfg_type("Secrets"))
    self._cfg_type = "ConfigMaps"
    self._style_cfg_toggle()
    tb_lay.addWidget(self.cfg_type_toggle)

    self.cfg_filter = QLineEdit()
    self.cfg_filter.setPlaceholderText(" Filter…")
    self.cfg_filter.textChanged.connect(self._filter_configs)
    tb_lay.addWidget(self.cfg_filter, 1)
    ll.addWidget(self.cfg_type_bar)

    # Cards instead of bare text rows — icon, name, and a ConfigMap/
    # Secret pill per entry, spaced out like the Pods/Deployments
    # lists (k8s_cards.py) instead of one dense column of names.
    self.cfg_list = QListWidget()
    self.cfg_list.setSpacing(6)
    self.cfg_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.cfg_list.currentItemChanged.connect(self._on_cfg_selection_changed)
    self.cfg_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 10px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    ll.addWidget(self.cfg_list)
    splitter.addWidget(left)

    # Right: detail + raw yaml
    right = QWidget()
    rl = QVBoxLayout(right)
    rl.setContentsMargins(0, 0, 0, 0)
    rl.setSpacing(0)

    self.cfg_detail_hdr = QLabel(" Data")
    self.cfg_detail_hdr.setFixedHeight(34)
    self.cfg_detail_hdr.setStyleSheet(
      f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
      f"font-weight: 700; border-bottom: 1px solid {T['BORDER']}; padding-left: 14px;"
    )
    rl.addWidget(self.cfg_detail_hdr)

    self.cfg_detail = QTreeWidget()
    self._style_tree(self.cfg_detail)
    self.cfg_detail.setRootIsDecorated(False)
    self.cfg_detail.setAlternatingRowColors(True)
    self.cfg_detail.setColumnCount(2)
    self.cfg_detail.setHeaderLabels(["Key", "Value"])
    self.cfg_detail.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
    self.cfg_detail.header().setSectionResizeMode(1, QHeaderView.Stretch)
    rl.addWidget(self.cfg_detail)

    self.cfg_raw_lbl = QLabel(" Structured View")
    self.cfg_raw_lbl.setFixedHeight(34)
    self.cfg_raw_lbl.setStyleSheet(
      f"background: {T['BG_PANEL']}; color: {T['TEXT_DIM']}; font-size: 13px; "
      f"font-weight: 700; border-top: 1px solid {T['BORDER']}; "
      f"border-bottom: 1px solid {T['BORDER']}; padding-left: 14px;"
    )
    rl.addWidget(self.cfg_raw_lbl)

    self.cfg_raw = QTextEdit()
    self.cfg_raw.setReadOnly(True)
    self.cfg_raw.setFont(monospace_font(11))
    self.cfg_raw.setMinimumHeight(220)
    self.cfg_raw.setStyleSheet(f"padding: 10px; border: none; background: {T['BG_DARK']};")
    rl.addWidget(self.cfg_raw)

    splitter.addWidget(right)
    splitter.setSizes([320, 620])
    lay.addWidget(splitter)
    add_icon_tab(self.sub_tabs, w, "Config && Secrets")


  def _style_cfg_toggle(self):
    """Pill-shaped container + two checkable buttons that look like one
    segmented control (selected side lit with the accent colour).
    Re-called from apply_theme() since colours are literal hex here."""
    self.cfg_type_toggle.setStyleSheet(
      f"QWidget#cfg_type_toggle {{ background: {T['BG_ITEM']}; "
      f"border: 1px solid {T['BORDER']}; border-radius: 18px; }}"
    )
    btn_css = f"""
      QPushButton {{
        background: transparent; color: {T['TEXT_DIM']};
        border: none; border-radius: 15px; padding: 0 16px;
        font-size: 12px; font-weight: 700;
      }}
      QPushButton:hover:!checked {{ background: {T['BG_HOVER']}; color: {T['TEXT_PRIMARY']}; }}
      QPushButton:checked {{ background: {T['ACCENT']}; color: white; }}
    """
    self.cfg_type_cm_btn.setStyleSheet(btn_css)
    self.cfg_type_secret_btn.setStyleSheet(btn_css)


  def _set_cfg_type(self, name: str):
    """Click handler for the ConfigMaps/Secrets segmented toggle —
    keeps the two buttons mutually exclusive (QPushButton's own
    setCheckable doesn't do this on its own outside a QButtonGroup)
    and reloads the list for the newly-selected type."""
    self._cfg_type = name
    self.cfg_type_cm_btn.setChecked(name == "ConfigMaps")
    self.cfg_type_secret_btn.setChecked(name == "Secrets")
    self._load_config_resources()


  def _build_events_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    tb = QHBoxLayout()
    tb.setContentsMargins(10, 6, 10, 6)
    tb.setSpacing(8)
    self.event_filter = QLineEdit()
    self.event_filter.setPlaceholderText(" Filter events (reason / object / message)…")
    self.event_filter.setMaximumWidth(280)
    self.event_filter.textChanged.connect(self._filter_events)
    tb.addWidget(self.event_filter)

    self.event_count_lbl = QLabel("")
    tb.addWidget(self.event_count_lbl)
    tb.addStretch()

    self.event_warn_btn = self._toolbar_btn(" Warnings only")
    self.event_warn_btn.setCheckable(True)
    self.event_warn_btn.toggled.connect(self._toggle_events_warnings_only)
    tb.addWidget(self.event_warn_btn)

    tb_widget = QWidget()
    tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
    tb_widget.setLayout(tb)
    lay.addWidget(tb_widget)
    self.events_toolbar = tb_widget

    # Newest first, warnings visually distinct — see EventCardWidget
    # (k8s_cards.py) for the accent-color logic.
    self.event_list = QListWidget()
    self.event_list.setSpacing(4)
    self.event_list.setSelectionMode(QAbstractItemView.SingleSelection)
    self.event_list.setContextMenuPolicy(Qt.CustomContextMenu)
    self.event_list.customContextMenuRequested.connect(self._event_ctx_menu)
    self.event_list.itemDoubleClicked.connect(self._on_event_double_click)
    self.event_list.currentItemChanged.connect(self._on_event_selection_changed)
    self.event_list.setStyleSheet(
      "QListWidget { background: transparent; border: none; padding: 8px; }"
      "QListWidget::item { border: none; padding: 0; margin: 0; }"
    )
    lay.addWidget(self.event_list)
    add_icon_tab(self.sub_tabs, w, " Events")


  def _build_terminal_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(8, 8, 8, 8)
    lay.setSpacing(8)

    self.k8s_terminal = QTextEdit()
    self.k8s_terminal.setReadOnly(True)
    self.k8s_terminal.setFont(monospace_font(11))
    self.k8s_terminal.setStyleSheet(
      f"background: #0d0d1a; color: {T['SUCCESS']}; border: none; padding: 8px;"
    )
    self.k8s_terminal.setPlaceholderText("kubectl output appears here…")
    lay.addWidget(self.k8s_terminal)

    inp_row = QHBoxLayout()
    self.k8s_inp = QLineEdit()
    self.k8s_inp.setPlaceholderText("kubectl …")
    self.k8s_inp.returnPressed.connect(self._run_kubectl_terminal)
    inp_row.addWidget(self.k8s_inp)

    clr = self._toolbar_btn("Clear")
    clr.clicked.connect(self.k8s_terminal.clear)
    inp_row.addWidget(clr)

    run = self._toolbar_btn("Run", object_name="primary")
    run.clicked.connect(self._run_kubectl_terminal)
    inp_row.addWidget(run)
    lay.addLayout(inp_row)
    add_icon_tab(self.sub_tabs, w, "Terminal")


  def _build_tunnels_tab(self):
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)

    # Toolbar: CSV path + reload + select-all/clear
    tb = QHBoxLayout()
    tb.setContentsMargins(10, 6, 10, 6)
    tb.setSpacing(8)

    self.tunnel_path_lbl = QLabel(f" {self._tunnel_csv_path} (on VM)")
    self.tunnel_path_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 13px;")
    tb.addWidget(self.tunnel_path_lbl)
    tb.addStretch()

    change_file_btn = self._toolbar_btn(
      " Change File",
      tooltip=(
        "Point at a different tunnel-services CSV on the connected VM\n"
        "(e.g. a personal or per-project file instead of the shared default).\n"
        "Remembered for next time."
      ),
    )
    change_file_btn.clicked.connect(self._change_tunnel_csv_path)
    tb.addWidget(change_file_btn)

    reload_btn = self._toolbar_btn("↺ Reload CSV")
    reload_btn.clicked.connect(self._load_tunnel_csv)
    tb.addWidget(reload_btn)

    manage_btn = self._toolbar_btn(
      " Manage Services",
      tooltip="Add, edit, or remove tunnel services stored on the connected VM",
    )
    manage_btn.clicked.connect(self._open_manage_tunnel_services)
    tb.addWidget(manage_btn)

    refresh_status_btn = self._toolbar_btn(
      " Refresh Status",
      tooltip=(
        "Check which services' ports are currently listening on the VM\n"
        "( exposed / not exposed), without reloading the CSV."
      ),
    )
    refresh_status_btn.clicked.connect(self._refresh_tunnel_status)
    tb.addWidget(refresh_status_btn)

    selall_btn = self._toolbar_btn(" Select All")
    selall_btn.clicked.connect(lambda: self._set_all_tunnel_checks(True))
    tb.addWidget(selall_btn)

    clear_btn = self._toolbar_btn(" Clear")
    clear_btn.clicked.connect(lambda: self._set_all_tunnel_checks(False))
    tb.addWidget(clear_btn)

    tb_widget = QWidget()
    tb_widget.setStyleSheet(f"background: {T['BG_PANEL']}; border-bottom: 1px solid {T['BORDER']};")
    tb_widget.setLayout(tb)
    lay.addWidget(tb_widget)
    self.tunnel_toolbar = tb_widget
    
    search_row = QHBoxLayout()
    search_row.setContentsMargins(10, 6, 10, 6)
    search_row.setSpacing(8)

    self.tunnel_search = QLineEdit()
    self.tunnel_search.setPlaceholderText(" Filter services...")
    self.tunnel_search.setClearButtonEnabled(True)
    self.tunnel_search.setMaximumHeight(34)
    self.tunnel_search.textChanged.connect(self._filter_tunnel_services)
    search_row.addWidget(self.tunnel_search, 1)

    # Status toggle — All / Active () / Inactive (). Mutually
    # exclusive via QButtonGroup, combined with the text search above
    # in _filter_tunnel_services() rather than replacing it.
    self.tunnel_filter_all_btn = self._toolbar_btn(
      "All", tooltip="Show every service, regardless of status")
    self.tunnel_filter_active_btn = self._toolbar_btn(
      " Active", tooltip="Show only services currently exposed on the VM")
    self.tunnel_filter_inactive_btn = self._toolbar_btn(
      " Inactive", tooltip="Show only services not currently exposed on the VM")

    self._tunnel_filter_keys = {}
    self.tunnel_filter_group = QButtonGroup(self)
    self.tunnel_filter_group.setExclusive(True)
    for btn, key in (
      (self.tunnel_filter_all_btn, "all"),
      (self.tunnel_filter_active_btn, "active"),
      (self.tunnel_filter_inactive_btn, "inactive"),
    ):
      btn.setCheckable(True)
      self.tunnel_filter_group.addButton(btn)
      self._tunnel_filter_keys[btn] = key
      search_row.addWidget(btn)
    self.tunnel_filter_all_btn.setChecked(True)
    self.tunnel_filter_group.buttonClicked.connect(self._on_tunnel_status_filter_clicked)

    lay.addLayout(search_row)
    # Tunnel cards (Manage Services card style); status dot per card
    self.tunnel_list = TunnelCardGrid()
    self.tunnel_list.selection_changed.connect(self._update_tunnel_cmd_preview)
    self.tunnel_list.set_empty_text("Connect to a VM to see its tunnel services")
    lay.addWidget(self.tunnel_list, 1)

    # Lightweight live status: re-check VM listening ports every 15s, only
    # while the Tunnels tab is on screen (event-driven refreshes also run
    # after start/stop/restart/kill).
    self._tunnel_status_timer = QTimer(self)
    self._tunnel_status_timer.setInterval(15000)
    self._tunnel_status_timer.timeout.connect(self._poll_tunnel_status)
    self._tunnel_status_timer.start()

    # Command preview (read-only, for transparency/debugging)
    preview_row = QHBoxLayout()
    preview_row.setContentsMargins(10, 8, 10, 4)
    preview_row.addWidget(QLabel("Command:"))
    self.tunnel_cmd_preview = QLineEdit()
    self.tunnel_cmd_preview.setReadOnly(True)
    self.tunnel_cmd_preview.setFont(monospace_font(10))
    self.tunnel_cmd_preview.setPlaceholderText("Select service(s) below to preview the SSH tunnel command…")
    preview_row.addWidget(self.tunnel_cmd_preview, 1)
    lay.addLayout(preview_row)

    # Controls: status + start/stop
    ctrl_row = QHBoxLayout()
    ctrl_row.setContentsMargins(10, 4, 10, 10)
    ctrl_row.setSpacing(8)

    self.tunnel_status_lbl = QLabel("● Not tunnelling")
    self.tunnel_status_lbl.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
    ctrl_row.addWidget(self.tunnel_status_lbl)
    ctrl_row.addStretch()

    self.tunnel_start_btn = self._toolbar_btn(" Tunnel", object_name="primary")
    self.tunnel_start_btn.clicked.connect(self._start_tunnel)
    ctrl_row.addWidget(self.tunnel_start_btn)

    self.tunnel_stop_btn = self._toolbar_btn("⏹ Stop", object_name="danger")
    self.tunnel_stop_btn.setEnabled(False)
    self.tunnel_stop_btn.clicked.connect(self._stop_tunnel)
    ctrl_row.addWidget(self.tunnel_stop_btn)

    self.port_kill = self._toolbar_btn(" Kill Port", object_name="danger")
    self.port_kill.clicked.connect(self._kill_selected_ports)
    ctrl_row.addWidget(self.port_kill)

    self.tunnel_restart_btn = self._toolbar_btn(
      " Restart Tunneling",
      tooltip=(
        "Runs 'kubectl port-forward' directly on the connected VM for each\n"
        "selected service, e.g.:\n"
        "nohup kubectl -n <namespace> port-forward svc/<name> <port>:<port> &\n\n"
        "This is separate from the local SSH tunnel above — use both together:\n"
        "this exposes the service on the VM's own localhost, and the SSH\n"
        "tunnel forwards that port to your machine."
      ),
    )
    self.tunnel_restart_btn.clicked.connect(self._restart_kubectl_tunnels)
    ctrl_row.addWidget(self.tunnel_restart_btn)

    lay.addLayout(ctrl_row)

    # Process log (ssh stdout/stderr, merged)
    self.tunnel_log = QTextEdit()
    self.tunnel_log.setReadOnly(True)
    self.tunnel_log.setFont(monospace_font(10))
    self.tunnel_log.setFixedHeight(130)
    self.tunnel_log.setPlaceholderText("Tunnel process output appears here…")
    self.tunnel_log.setStyleSheet(
      f"background: #0d0d1a; color: {T['TEXT_DIM']}; border: none; padding: 8px;"
    )
    lay.addWidget(self.tunnel_log)

    add_icon_tab(self.sub_tabs, w, " Tunnels")
    # self._load_tunnel_csv()

    # Apply whatever tab-visibility choices were saved in Settings
    # (defaults to "everything visible" the first time the app runs).
    self._apply_saved_hidden_tabs()

  # ── Tab visibility (Settings → Kubernetes Tabs) ────────────

