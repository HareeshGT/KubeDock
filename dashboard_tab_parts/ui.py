from dashboard_tab import *
from PyQt5.QtWidgets import QStackedWidget, QHeaderView
from dashboard_tab import (
  _dashboard_text, _pct_color, _NodeGridContainer,
)

"""DashboardTab implementation mixin.

The public DashboardTab class remains in dashboard_tab.py; this module only
contains logically grouped DashboardTab methods.
"""

class DashboardUIMixin:
  def _build_ui(self):
      root = QVBoxLayout(self)
      root.setContentsMargins(0, 0, 0, 0)
      root.setSpacing(0)
  
      self.ctrl_bar = QWidget()
      self.ctrl_bar.setObjectName("dash_ctrl")
      self.ctrl_bar.setFixedHeight(68)
      cb = QHBoxLayout(self.ctrl_bar)
      # Keep the header's trailing controls inside the available viewport.
      # The title is allowed to elide before the status/actions are pushed off-screen.
      cb.setContentsMargins(12, 8, 12, 8)
      cb.setSpacing(8)
  
      # Page header: 22px title + secondary subtitle (design-system hierarchy)
      title_col = QVBoxLayout()
      title_col.setSpacing(0)
      self.page_title_lbl = QLabel("Connected Machine")
      self.page_sub_lbl = QLabel("Live host and Kubernetes infrastructure")
      for label in (self.page_title_lbl, self.page_sub_lbl):
        label.setMinimumWidth(0)
        label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
      title_col_widget = QWidget()
      title_col_widget.setLayout(title_col)
      title_col_widget.setMinimumWidth(0)
      title_col_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
      cb.addWidget(title_col_widget, 1)
  
      self.updated_lbl = QLabel("")
      self.updated_lbl.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
      cb.addWidget(self.updated_lbl)
  
      self.live_lbl = QLabel("Not connected")
      cb.addWidget(self.live_lbl)
  
      self.refresh_btn = icon_button(" Refresh")
      self.refresh_btn.setObjectName("primary")
      self.refresh_btn.setFixedHeight(28)
      self.refresh_btn.clicked.connect(self._refresh_current_view)
      cb.addWidget(self.refresh_btn)
  
      root.addWidget(self.ctrl_bar)
  
      scroll = QScrollArea()
      scroll.setWidgetResizable(True)
      scroll.setFrameShape(QFrame.NoFrame)
      content = QWidget()
      self._content_layout = QVBoxLayout(content)
      self._content_layout.setContentsMargins(16, 16, 16, 16)
      self._content_layout.setSpacing(16)
  
      self.disconnected_lbl = QLabel("Connect to an instance to see its dashboard.")
      self.disconnected_lbl.setAlignment(Qt.AlignCenter)
      self.disconnected_lbl.setStyleSheet(f"color: {_dashboard_text('muted')}; padding: 40px; font-size: 13px;")
      self._content_layout.addWidget(self.disconnected_lbl)
  
      # ── Instance card ──────────────────────────────────
      self.vm_card = self._make_card(" Instance")
      vm_body = QVBoxLayout()
      vm_body.setSpacing(10)
  
      # Top row: CPU/Memory rings on the left, instance detail fields on
      # the right — previously the fields sat in their own row above the
      # rings, leaving the whole right-hand side of the rings row empty.
      top_row = QHBoxLayout()
      top_row.setSpacing(40)
  
      rings_row = QHBoxLayout()
      rings_row.setSpacing(48)
      self.cpu_ring = self._labeled_ring("CPU usage")
      rings_row.addLayout(self.cpu_ring["layout"])
      self.mem_ring = self._labeled_ring("Memory usage")
      rings_row.addLayout(self.mem_ring["layout"])
      self.storage_ring = self._labeled_ring("Storage usage")
      rings_row.addLayout(self.storage_ring["layout"])
      top_row.addLayout(rings_row)
  
      divider = QFrame()
      divider.setFrameShape(QFrame.VLine)
      divider.setStyleSheet(f"color: {T['BORDER']};")
      top_row.addWidget(divider)
  
      grid = QGridLayout()
      grid.setHorizontalSpacing(24)
      grid.setVerticalSpacing(10)
      self._vm_fields = {}
      for row, (key, label) in enumerate([
        ("hostname", "Hostname"), ("os", "OS"),
        ("kernel", "Kernel"), ("uptime", "Uptime"),
        ("load", "Load average"), ("cpu_model", "CPU"),
      ]):
        lk = QLabel(label + ":")
        lk.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
        lv = QLabel("—")
        lv.setStyleSheet(f"color: {_dashboard_text('primary')}; font-size: 12px;")
        lv.setWordWrap(True)
        grid.addWidget(lk, row, 0)
        grid.addWidget(lv, row, 1)
        self._vm_fields[key] = lv
      grid.setColumnStretch(1, 1)
      top_row.addLayout(grid, 1)
  
      vm_body.addLayout(top_row)
  
      disk_lbl = QLabel("Disk")
      disk_lbl.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px; font-weight: 700;")
      vm_body.addWidget(disk_lbl)
  
      self.disk_tree = QTreeWidget()
      self.disk_tree.setHeaderLabels(["Filesystem", "Size", "Used", "Avail", "Use", "Mounted on"])
      self.disk_tree.setRootIsDecorated(False)
      self.disk_tree.setUniformRowHeights(True)
      self.disk_tree.setMinimumHeight(90)
      self.disk_tree.setMaximumHeight(160)
      vm_body.addWidget(self.disk_tree)
  
      self.vm_card["body"].addLayout(vm_body)
      self._content_layout.addWidget(self.vm_card["frame"])

      # ── Live process monitor ─────────────────────────────
      self.process_card = self._make_card(" Live Processes")
      process_body = self.process_card["body"]
      process_body.setSpacing(10)

      process_header = QHBoxLayout()
      process_header.setSpacing(8)
      self.process_status = QLabel("Waiting for SSH connection…")
      self.process_status.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 11px;")
      process_header.addWidget(self.process_status)
      process_header.addStretch()
      self.process_interval_lbl = QLabel("LIVE · 1s")
      process_header.addWidget(self.process_interval_lbl)
      process_body.addLayout(process_header)

      # Compact system summary. The old implementation displayed the raw
      # `top` terminal output, which was hard to scan and looked different
      # between Linux and macOS. These fields are populated from the same
      # snapshot, so the dashboard remains lightweight while presenting the
      # important information consistently.
      self.process_metrics = {}
      self._process_tiles = []
      metric_row = QHBoxLayout()
      metric_row.setSpacing(8)
      for key, label in [
        ("uptime", "Uptime"), ("load", "Load"), ("tasks", "Processes"),
        ("cpu", "CPU"), ("memory", "Memory"),
      ]:
        tile = QFrame()
        tile.setObjectName("process_metric_tile")
        tl = QVBoxLayout(tile)
        tl.setContentsMargins(10, 7, 10, 7)
        tl.setSpacing(1)
        value = QLabel("—")
        caption = QLabel(label)
        self._process_tiles.append((tile, value, caption))
        tl.addWidget(value)
        tl.addWidget(caption)
        metric_row.addWidget(tile, 1)
        self.process_metrics[key] = value
      process_body.addLayout(metric_row)

      self.process_detail_lbl = QLabel("CPU — · Memory — · Load —")
      process_body.addWidget(self.process_detail_lbl)

      self.process_tree = QTreeWidget()
      self.process_tree.setColumnCount(7)
      self.process_tree.setHeaderLabels([
        "PID", "USER", "CPU %", "MEM %", "STATE", "TIME", "COMMAND"
      ])
      self.process_tree.setRootIsDecorated(False)
      self.process_tree.setUniformRowHeights(True)
      self.process_tree.setAlternatingRowColors(True)
      self.process_tree.setSortingEnabled(True)
      self.process_tree.sortItems(2, Qt.DescendingOrder)
      self.process_tree.setMinimumHeight(255)
      self.process_tree.setMaximumHeight(390)
      header = self.process_tree.header()
      header.setStretchLastSection(True)
      header.setDefaultSectionSize(70)
      header.resizeSection(0, 72)
      header.resizeSection(1, 105)
      header.resizeSection(2, 65)
      header.resizeSection(3, 65)
      header.resizeSection(4, 60)
      header.resizeSection(5, 78)
      self.process_tree.setColumnWidth(6, 420)
      process_body.addWidget(self.process_tree)
      self._style_process_card()

      self._content_layout.addWidget(self.process_card["frame"])

      # ── FTP / FTPS device dashboard ───────────────────────
      self.ftp_card = self._make_card(" FTP / FTPS Device")
      ftp_outer = self.ftp_card["body"]
  
      def add_metric_row(parent_layout, title, pairs):
        row = QHBoxLayout()
        row.setSpacing(10)
        for key, label in pairs:
          tile = QFrame()
          tile.setObjectName("ftp_metric_tile")
          tl = QVBoxLayout(tile)
          tl.setContentsMargins(12, 9, 12, 9)
          tl.setSpacing(2)
          value = QLabel("—")
          value.setAlignment(Qt.AlignCenter)
          value.setStyleSheet(f"color: {_dashboard_text('primary')}; font-size: 17px; font-weight: 700;")
          caption = QLabel(label)
          caption.setAlignment(Qt.AlignCenter)
          caption.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 10px; font-weight: 600;")
          tl.addWidget(value)
          tl.addWidget(caption)
          row.addWidget(tile, 1)
          self._ftp_fields[key] = value
        parent_layout.addLayout(row)
  
      add_metric_row(ftp_outer, "", [
        ("protocol", "Protocol"), ("host", "Server"), ("port", "Port"), ("user", "Username"),
      ])
      add_metric_row(ftp_outer, "", [
        ("security", "Security"), ("mode", "Transfer mode"), ("latency", "Control latency"), ("uptime", "Session uptime"),
      ])
  
      conn_grid = QGridLayout()
      conn_grid.setHorizontalSpacing(24)
      conn_grid.setVerticalSpacing(8)
      for row, (key, label) in enumerate([
        ("cwd", "Current directory"), ("system", "Server system"),
        ("encoding", "Encoding"), ("timeout", "Timeout"),
        ("tls_version", "TLS version"), ("tls_cipher", "TLS cipher"),
      ]):
        lk = QLabel(label + ":")
        lk.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
        lv = QLabel("—")
        lv.setWordWrap(True)
        lv.setStyleSheet(f"color: {_dashboard_text('primary')}; font-size: 12px;")
        conn_grid.addWidget(lk, row // 2, (row % 2) * 2)
        conn_grid.addWidget(lv, row // 2, (row % 2) * 2 + 1)
        conn_grid.setColumnStretch((row % 2) * 2 + 1, 1)
        self._ftp_fields[key] = lv
      ftp_outer.addLayout(conn_grid)
  
      dir_label = QLabel("Current directory")
      dir_label.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px; font-weight: 700;")
      ftp_outer.addWidget(dir_label)
      add_metric_row(ftp_outer, "", [
        ("items", "Items"), ("files", "Files"), ("folders", "Folders"), ("total", "Listed size"),
      ])
  
      insight_grid = QGridLayout()
      insight_grid.setHorizontalSpacing(24)
      insight_grid.setVerticalSpacing(8)
      for row, (key, label) in enumerate([
        ("largest", "Largest file"), ("newest", "Newest file"),
      ]):
        lk = QLabel(label + ":")
        lk.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
        lv = QLabel("—")
        lv.setWordWrap(True)
        lv.setStyleSheet(f"color: {_dashboard_text('primary')}; font-size: 12px;")
        insight_grid.addWidget(lk, row, 0)
        insight_grid.addWidget(lv, row, 1)
        insight_grid.setColumnStretch(1, 1)
        self._ftp_fields[key] = lv
      ftp_outer.addLayout(insight_grid)
  
      activity_label = QLabel("Session activity")
      activity_label.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px; font-weight: 700;")
      ftp_outer.addWidget(activity_label)
      add_metric_row(ftp_outer, "", [
        ("downloaded", "Downloaded"), ("uploaded", "Uploaded"),
        ("download_count", "Downloads"), ("upload_count", "Uploads"),
      ])
      last_row = QHBoxLayout()
      last_lbl = QLabel("Last transfer:")
      last_lbl.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
      last_value = QLabel("—")
      last_value.setStyleSheet(f"color: {_dashboard_text('primary')}; font-size: 12px;")
      self._ftp_fields["last_operation"] = last_value
      last_row.addWidget(last_lbl)
      last_row.addWidget(last_value, 1)
      ftp_outer.addLayout(last_row)
  
      self.ftp_features = QLabel("")
      self.ftp_features.setWordWrap(True)
      self.ftp_features.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 11px;")
      ftp_outer.addWidget(self.ftp_features)
  
      ftp_note = QLabel("CPU, RAM, OS and system disk metrics require an OS-level interface such as SSH; standard FTP does not expose them reliably.")
      ftp_note.setWordWrap(True)
      ftp_note.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 11px;")
      ftp_outer.addWidget(ftp_note)
  
      self._content_layout.addWidget(self.ftp_card["frame"])
  
      # ── Kubernetes cluster overview ─────────────────────
      self.k8s_summary_card = self._make_card(" Kubernetes Overview", icon="kubernetes-white")
      summary_grid = QGridLayout()
      summary_grid.setHorizontalSpacing(12)
      summary_grid.setVerticalSpacing(10)
      self._k8s_summary = {}
      summary_specs = [
        ("nodes", "Nodes"), ("pods", "Pods"), ("running", "Running"), ("pending", "Pending"),
        ("failed", "Failed"), ("namespaces", "Namespaces"), ("pressure", "Pressure"), ("metrics", "Metrics"),
      ]
      for i, (key, label) in enumerate(summary_specs):
        tile = QFrame()
        tile.setObjectName("k8s_summary_tile")
        tile_layout = QVBoxLayout(tile)
        tile_layout.setContentsMargins(12, 8, 12, 8)
        tile_layout.setSpacing(2)
        value = QLabel("—")
        value.setAlignment(Qt.AlignCenter)
        value.setStyleSheet(f"color: {_dashboard_text('primary')}; font-size: 17px; font-weight: 700;")
        caption = QLabel(label)
        caption.setAlignment(Qt.AlignCenter)
        caption.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 10px; font-weight: 600;")
        tile_layout.addWidget(value)
        tile_layout.addWidget(caption)
        summary_grid.addWidget(tile, i // 4, i % 4)
        self._k8s_summary[key] = value
      self.k8s_summary_card["body"].addLayout(summary_grid)
      self._content_layout.addWidget(self.k8s_summary_card["frame"])
  
      # ── Phase 2: workloads ─────────────────────────────
      self.workloads_card = self._make_card(" Workloads")
      self.workloads_tree = QTreeWidget()
      self.workloads_tree.setHeaderLabels([
        "Kind", "Namespace", "Name", "Ready", "Desired",
        "Available", "Updated", "Unavailable", "Age"
      ])
      self._style_tree(self.workloads_tree)
      self.workloads_tree.setMinimumHeight(130)
      self.workloads_tree.setMaximumHeight(260)
      self.workloads_card["body"].addWidget(self.workloads_tree)
      self._content_layout.addWidget(self.workloads_card["frame"])
  
      # ── Phase 2: services ──────────────────────────────
      self.services_card = self._make_card(" Services")
      self.services_tree = QTreeWidget()
      self.services_tree.setHeaderLabels([
        "Service", "Namespace", "Type", "Cluster IP",
        "External", "Ports", "Endpoints", "Age"
      ])
      self._style_tree(self.services_tree)
      self.services_tree.setMinimumHeight(110)
      self.services_tree.setMaximumHeight(240)
      self.services_card["body"].addWidget(self.services_tree)
      self._content_layout.addWidget(self.services_card["frame"])
  
      # ── Phase 2: recent Kubernetes events ───────────────
      self.events_card = self._make_card(" Recent Kubernetes Events", icon="kubernetes-white")
      self.events_tree = QTreeWidget()
      self.events_tree.setHeaderLabels([
        "Time", "Type", "Reason", "Object", "Namespace", "Message"
      ])
      self._style_tree(self.events_tree)
      self.events_tree.setMinimumHeight(130)
      self.events_tree.setMaximumHeight(300)
      self.events_card["body"].addWidget(self.events_tree)
      self._content_layout.addWidget(self.events_card["frame"])
  
      # ── Kubernetes nodes card ────────────────────────────
      # Nodes only — pods used to be shown as inline expandable children
      # of each node, which made this card feel cramped. They now live in
      # a separate NodeDetailWindow opened per-node (see _on_node_double_
      # clicked), so this table stays one clean row per node.
      self.k8s_card = self._make_card(" Kubernetes Nodes", icon="kubernetes-white")
      self.k8s_note = QLabel("")
      self.k8s_note.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
      self.k8s_note.hide()
      self.k8s_card["body"].addWidget(self.k8s_note)
  
      self.k8s_hint = QLabel("Double-click a node card to see the pods running on it.")
      self.k8s_hint.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
      self.k8s_card["body"].addWidget(self.k8s_hint)
  
      # Node summaries as cards, 3 per row, instead of a one-row-per-node
      # table. self.k8s_grid is repopulated from scratch on every refresh
      # (see _clear_node_grid / _add_node_card). The container is a
      # resize-aware widget so the row of cards keeps filling the full
      # section width as the window is resized (see _rescale_node_cards).
      self.k8s_grid_container = _NodeGridContainer()
      self.k8s_grid = QGridLayout(self.k8s_grid_container)
      self.k8s_grid.setContentsMargins(0, 20, 0, 0)
      self.k8s_grid.setHorizontalSpacing(16)
      self.k8s_grid.setVerticalSpacing(16)
      # Cards are square and fixed-size at any given moment, so the card
      # columns shouldn't stretch themselves (that would space cards
      # unevenly). Cards live in columns 1..NODE_GRID_COLS; column 0 and
      # the trailing column split any leftover width evenly between
      # them, which centres the row instead of pinning it to the left.
      self._node_grid_cols = 2
      self.k8s_grid.setColumnStretch(0, 1)
      self.k8s_grid.setColumnStretch(self._node_grid_cols + 1, 1)
      self.k8s_grid_container.setMinimumWidth(0)
      self.k8s_grid_container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
      self.k8s_grid_container.resized.connect(self._rescale_node_cards)
      self.k8s_card["body"].addWidget(self.k8s_grid_container)
      self._content_layout.addWidget(self.k8s_card["frame"])
  
      # ── Phase 7: historical monitoring ──────────────────────
      self.history_card = self._make_card(" Historical Monitoring")
      hb = self.history_card["body"]
      self.history_status = QLabel("Collecting history…")
      self.history_status.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
      hb.addWidget(self.history_status)
  
      grid = QGridLayout()
      self.history_cpu = HistoryChart("Average node CPU (%)")
      self.history_mem = HistoryChart("Average node memory (%)")
      self.history_pods = HistoryChart("Total pods")
      self.history_nodes = HistoryChart("Ready nodes")
      grid.addWidget(self.history_cpu, 0, 0)
      grid.addWidget(self.history_mem, 0, 1)
      grid.addWidget(self.history_pods, 1, 0)
      grid.addWidget(self.history_nodes, 1, 1)
      hb.addLayout(grid)
  
      self.history_alerts = QLabel("Alerts: none")
      self.history_alerts.setWordWrap(True)
      self.history_alerts.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
      hb.addWidget(self.history_alerts)
  
      self.history_events = QTreeWidget()
      self.history_events.setHeaderLabels(["Time", "Type", "Reason", "Object", "Namespace", "Message"])
      self._style_tree(self.history_events)
      self.history_events.setMinimumHeight(120)
      self.history_events.setMaximumHeight(240)
      hb.addWidget(self.history_events)
      self._content_layout.addWidget(self.history_card["frame"])
  
      self._render_history()
      self._content_layout.addStretch()
      scroll.setWidget(content)
      # Dashboard now has two focused views: the shared application
      # connection/runtime view and the connected machine view. Keep the
      # existing machine dashboard intact inside its own stacked page.
      self.dashboard_view_bar = QWidget()
      self.dashboard_view_bar.setObjectName("dashboard_view_bar")
      view_bar_layout = QHBoxLayout(self.dashboard_view_bar)
      view_bar_layout.setContentsMargins(16, 8, 16, 0)
      view_bar_layout.setSpacing(8)
      self.machine_view_btn = QPushButton(" Connected Machine ")
      self.machine_view_btn.setCheckable(True)
      self.machine_view_btn.setChecked(True)
      self.machine_view_btn.clicked.connect(lambda: self._set_dashboard_view(0))
      self.app_connections_view_btn = QPushButton(" App Connections ")
      self.app_connections_view_btn.setCheckable(True)
      self.app_connections_view_btn.clicked.connect(lambda: self._set_dashboard_view(1))
      view_bar_layout.addWidget(self.machine_view_btn)
      view_bar_layout.addWidget(self.app_connections_view_btn)
      view_bar_layout.addStretch(1)
      root.addWidget(self.dashboard_view_bar)

      self.dashboard_pages = QStackedWidget()
      self.dashboard_pages.addWidget(scroll)
      self._build_app_connections_page()
      root.addWidget(self.dashboard_pages, 1)
      self._app_connection_timer = QTimer(self)
      self._app_connection_timer.setInterval(2000)
      self._app_connection_timer.timeout.connect(self._refresh_app_connections)

      self.vm_card["frame"].hide()
      self.process_card["frame"].hide()
      self.k8s_summary_card["frame"].hide()
      self.workloads_card["frame"].hide()
      self.services_card["frame"].hide()
      self.events_card["frame"].hide()
      self.history_card["frame"].hide()
      self.k8s_card["frame"].hide()
      self.ftp_card["frame"].hide()
  
      self._apply_styles()
      for tile in self.findChildren(QFrame, "ftp_metric_tile"):
        tile.setStyleSheet(f"QFrame#ftp_metric_tile {{ background: {T['BG_PANEL']}; border: 1px solid {T['BORDER']}; border-radius: 10px; }}")
      # Palette the widgets above were styled with; apply_theme() remaps from it.
      self._styled_palette = snapshot_palette()
  
  
  def _build_app_connections_page(self):
      """Build the application-owned connection and service activity view."""
      scroll = QScrollArea()
      scroll.setWidgetResizable(True)
      scroll.setFrameShape(QFrame.NoFrame)
      content = QWidget()
      layout = QVBoxLayout(content)
      layout.setContentsMargins(16, 16, 16, 16)
      layout.setSpacing(16)

      self.app_connection_empty = QLabel(
        "No active remote connection. Connect to a server to see application connection details."
      )
      self.app_connection_empty.setAlignment(Qt.AlignCenter)
      self.app_connection_empty.setWordWrap(True)
      self.app_connection_empty.setStyleSheet(
        f"color: {_dashboard_text('muted')}; padding: 28px; font-size: 13px;"
      )
      layout.addWidget(self.app_connection_empty)

      self.app_connection_card = self._make_card(" Application Connection")
      grid = QGridLayout()
      grid.setHorizontalSpacing(12)
      grid.setVerticalSpacing(10)
      self._app_connection_values = {}
      for index, (key, caption) in enumerate((
        ("state", "Connection status"),
        ("protocol", "Protocol"),
        ("target", "Remote endpoint"),
        ("identity", "Connected user"),
        ("uptime", "Session uptime"),
        ("latency", "Connection latency"),
        ("alias", "Connection profile"),
        ("generation", "Session generation"),
      )):
        tile = QFrame()
        tile.setObjectName("app_connection_tile")
        tile_layout = QVBoxLayout(tile)
        tile_layout.setContentsMargins(12, 9, 12, 9)
        tile_layout.setSpacing(3)
        value = QLabel("—")
        value.setWordWrap(True)
        caption_lbl = QLabel(caption)
        tile_layout.addWidget(value)
        tile_layout.addWidget(caption_lbl)
        value.setMinimumWidth(0)
        value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        caption_lbl.setMinimumWidth(0)
        tile.setMinimumWidth(0)
        tile.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        grid.addWidget(tile, index // 2, index % 2)
        self._app_connection_values[key] = (value, caption_lbl, tile)
      grid.setColumnStretch(0, 1)
      grid.setColumnStretch(1, 1)
      self.app_connection_card["body"].addLayout(grid)
      self.app_connection_error = QLabel("")
      self.app_connection_error.setWordWrap(True)
      self.app_connection_card["body"].addWidget(self.app_connection_error)
      layout.addWidget(self.app_connection_card["frame"])

      self.ssh_pool_card = self._make_card(" SSH Connection Pool")
      pool_grid = QGridLayout()
      pool_grid.setHorizontalSpacing(12)
      pool_grid.setVerticalSpacing(10)
      self._ssh_pool_values = {}
      for index, (key, caption) in enumerate((
        ("total", "Active SSH connections"),
        ("maximum", "Configured maximum"),
        ("secondary", "Secondary connections"),
        ("channels", "Active command channels"),
        ("capacity", "Secondary channel capacity"),
        ("utilization", "Channel utilization"),
      )):
        tile = QFrame()
        tile.setObjectName("app_connection_tile")
        tile_layout = QVBoxLayout(tile)
        tile_layout.setContentsMargins(12, 9, 12, 9)
        tile_layout.setSpacing(3)
        value = QLabel("—")
        caption_lbl = QLabel(caption)
        tile_layout.addWidget(value)
        tile_layout.addWidget(caption_lbl)
        value.setMinimumWidth(0)
        value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        caption_lbl.setMinimumWidth(0)
        tile.setMinimumWidth(0)
        tile.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        pool_grid.addWidget(tile, index // 2, index % 2)
        self._ssh_pool_values[key] = (value, caption_lbl, tile)
      pool_grid.setColumnStretch(0, 1)
      pool_grid.setColumnStretch(1, 1)
      self.ssh_pool_card["body"].addLayout(pool_grid)
      self.ssh_pool_policy_lbl = QLabel(
        "Hybrid pooling · least-loaded channel assignment · scales out on demand · idle secondary connections close automatically"
      )
      self.ssh_pool_policy_lbl.setWordWrap(True)
      self.ssh_pool_card["body"].addWidget(self.ssh_pool_policy_lbl)
      layout.addWidget(self.ssh_pool_card["frame"])

      self.service_activity_card = self._make_card(" Feature Activity")
      self.service_activity_tree = QTreeWidget()
      self.service_activity_tree.setColumnCount(3)
      self.service_activity_tree.setHeaderLabels(
        ["Application service", "Active operations", "Registered workers"]
      )
      self._style_tree(self.service_activity_tree)
      self.service_activity_tree.setMinimumWidth(0)
      self.service_activity_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
      self.service_activity_tree.header().setStretchLastSection(True)
      self.service_activity_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
      self.service_activity_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
      self.service_activity_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
      self.service_activity_tree.setRootIsDecorated(False)
      self.service_activity_tree.setAlternatingRowColors(True)
      self.service_activity_tree.setMinimumHeight(240)
      self.service_activity_tree.setMaximumHeight(390)
      self.service_activity_card["body"].addWidget(self.service_activity_tree)
      layout.addWidget(self.service_activity_card["frame"])
      layout.addStretch(1)
      scroll.setWidget(content)
      self.dashboard_pages.addWidget(scroll)

      self._refresh_app_connections()

  def _set_dashboard_view(self, index):
      """Switch between app-owned connections and the connected machine."""
      index = 1 if int(index) == 1 else 0
      self.dashboard_pages.setCurrentIndex(index)
      self.machine_view_btn.setChecked(index == 0)
      self.app_connections_view_btn.setChecked(index == 1)
      if index == 0:
        self.page_title_lbl.setText(" Connected Machine ")
        self.page_sub_lbl.setText("Live host and Kubernetes infrastructure")
        self._app_connection_timer.stop()
        if self._active and self.ssh:
          self._resume_focused()
          self._timer.start()
          self._process_timer.start()
        elif self._active and self.ftp:
          self._refresh_ftp()
          self._timer.start()
      else:
        self.page_title_lbl.setText(" App Connections ")
        self.page_sub_lbl.setText("Shared connections, SSH pool and feature activity")
        self._timer.stop()
        self._process_timer.stop()
        self._refresh_app_connections()
        if self._active and (self.ssh or self.ftp):
          self._app_connection_timer.start()
      self._update_live_label()

  def _refresh_current_view(self):
      if getattr(self, "dashboard_pages", None) is not None and self.dashboard_pages.currentIndex() == 1:
        self._refresh_app_connections()
      else:
        self._refresh()

  def _refresh_app_connections(self):
      """Render a safe snapshot of ConnectionService and shared worker usage."""
      service = getattr(self, "dashboard_service", None)
      overview = service.connection_overview() if service is not None else {}
      state = str(overview.get("state") or "disconnected").lower()
      protocol = str(overview.get("protocol") or "—").upper()
      active = state in ("connected", "degraded")
      connected_at = overview.get("connected_at")
      uptime = "—"
      if connected_at:
        try:
          seconds = max(0, int(time.time() - float(connected_at)))
          days, remainder = divmod(seconds, 86400)
          hours, remainder = divmod(remainder, 3600)
          minutes, _ = divmod(remainder, 60)
          uptime = (f"{days}d " if days else "") + f"{hours}h {minutes}m"
        except (TypeError, ValueError, OverflowError):
          uptime = "—"
      latency = overview.get("latency_ms")
      values = {
        "state": state.replace("_", " ").title() if active else "Disconnected",
        "protocol": protocol,
        "target": (
          f"{overview.get('host', '—')}:{overview.get('port')}"
          if overview.get("port") else overview.get("host", "—")
        ),
        "identity": overview.get("username", "—"),
        "uptime": uptime,
        "latency": f"{float(latency):.1f} ms" if latency is not None else "Not measured",
        "alias": overview.get("alias", "—"),
        "generation": str(overview.get("generation", 0)),
      }
      for key, value in values.items():
        self._app_connection_values[key][0].setText(str(value))
      state_color = (
        T["SUCCESS"] if state == "connected"
        else T["WARNING"] if state == "degraded"
        else _dashboard_text("muted")
      )
      self._app_connection_values["state"][0].setStyleSheet(
        f"color: {state_color}; font-size: 16px; font-weight: 700;"
      )
      self.app_connection_empty.setVisible(not active)
      self.app_connection_error.setText(
        f"Last connection issue: {overview.get('last_error')}"
        if overview.get("last_error") else ""
      )
      self.app_connection_error.setStyleSheet(
        f"color: {T['DANGER']}; font-size: 11px;" if overview.get("last_error") else ""
      )

      pool = overview.get("pool") or {}
      is_ssh = active and protocol == "SSH"
      total = int(pool.get("total_connections", 0) or 0) if is_ssh else 0
      maximum = int(pool.get("max_connections", 5) or 5) if is_ssh else 5
      secondary = int(pool.get("secondary_connections", 0) or 0) if is_ssh else 0
      channels = int(pool.get("active_channels", 0) or 0) if is_ssh else 0
      capacity = int(pool.get("channel_capacity", 0) or 0) if is_ssh else 0
      utilization = f"{(channels / capacity) * 100:.0f}%" if capacity else "—"
      pool_values = {
        "total": f"{total} / {maximum}" if is_ssh else "Not applicable",
        "maximum": str(maximum) if is_ssh else "—",
        "secondary": str(secondary) if is_ssh else "—",
        "channels": str(channels) if is_ssh else "—",
        "capacity": str(capacity) if is_ssh else "—",
        "utilization": utilization if is_ssh else "—",
      }
      for key, value in pool_values.items():
        self._ssh_pool_values[key][0].setText(value)
      self.ssh_pool_card["frame"].setVisible(is_ssh)
      self.app_connection_card["frame"].setVisible(active)

      activity = overview.get("activity") or []
      tree = self.service_activity_tree
      tree.setUpdatesEnabled(False)
      tree.clear()
      for item in activity:
        from PyQt5.QtWidgets import QTreeWidgetItem
        row = QTreeWidgetItem([
          str(item.get("service", "—")),
          str(item.get("active_operations", 0)),
          str(item.get("registered_workers", 0)),
        ])
        tree.addTopLevelItem(row)
      tree.setUpdatesEnabled(True)
      self.service_activity_card["frame"].setVisible(active)
      self.updated_lbl.setText(f"Updated {time.strftime('%H:%M:%S')}")

  def _make_card(self, title: str, icon: str = None) -> dict:
      frame = QFrame()
      frame.setObjectName("dash_card")
      outer = QVBoxLayout(frame)
      outer.setContentsMargins(14, 10, 14, 14)
      outer.setSpacing(8)
      lbl = QLabel()
      name, cleaned = split_icon_text(title)
      if icon:
        # Explicit icon override (e.g. the white Kubernetes logo); the label
        # text is still the title, minus the leading space.
        name, cleaned = icon, title.strip()
      icon_lbl = None
      if name:
        set_label_icon(lbl, name, size=18)
        icon_lbl = lbl
        # Keep title in a compact row so the SVG never relies on an emoji font.
        title_row = QHBoxLayout()
        title_row.setSpacing(7)
        title_row.addWidget(lbl)
        text_lbl = QLabel(cleaned)
        text_lbl.setStyleSheet(f"color: {_dashboard_text('primary')}; font-size: 13px; font-weight: 700;")
        title_row.addWidget(text_lbl)
        title_row.addStretch()
        outer.addLayout(title_row)
        title_label = text_lbl
      else:
        lbl.setText(title)
        lbl.setStyleSheet(f"color: {_dashboard_text('primary')}; font-size: 13px; font-weight: 700;")
        outer.addWidget(lbl)
        title_label = lbl
      body = QVBoxLayout()
      outer.addLayout(body)
      card = {"frame": frame, "title_lbl": title_label, "body": outer,
              "icon_lbl": icon_lbl}
      # Remember every card so the header text/icon can be re-themed later.
      if not hasattr(self, "_dash_cards"):
        self._dash_cards = []
      self._dash_cards.append(card)
      return card
  
  
  def _labeled_ring(self, label: str) -> dict:
      """A vertical block — label on top, a large centred ring, value
      readout underneath. Two of these placed in a QHBoxLayout (see
      _build_ui) put CPU and Memory side by side instead of stacked."""
      col = QVBoxLayout()
      col.setSpacing(8)
  
      lk = QLabel(label)
      lk.setAlignment(Qt.AlignHCenter)
      lk.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px; font-weight: 700;")
      col.addWidget(lk)
  
      ring = CircularProgress(size=140, thickness=12, show_text=True, font_size=24)
      ring_row = QHBoxLayout()
      ring_row.addStretch(1)
      ring_row.addWidget(ring)
      ring_row.addStretch(1)
      col.addLayout(ring_row)
  
      spark = Sparkline()
      spark.setFixedWidth(120)
      spark_row = QHBoxLayout()
      spark_row.addStretch(1)
      spark_row.addWidget(spark)
      spark_row.addStretch(1)
      col.addLayout(spark_row)
  
      val_lbl = QLabel("—")
      val_lbl.setAlignment(Qt.AlignHCenter)
      val_lbl.setStyleSheet(f"color: {_dashboard_text('dim')}; font-size: 12px;")
      col.addWidget(val_lbl)
  
      return {"layout": col, "ring": ring, "spark": spark, "val_lbl": val_lbl}
  
  
  def _style_tree(self, tree):
      tree.setFont(monospace_font(12))
      tree.setStyleSheet(f"QTreeWidget {{ font-size: 12px; }} "
                f"QTreeWidget::item {{ height: 30px; }}")
  
  
  def _style_ring(self, ring: CircularProgress, pct: float):
      ring.setValue(pct, _pct_color(pct))
  
  
  def _clear_node_grid(self):
      """Remove and delete every card currently in the grid, ready for a
      fresh set to be added via _add_node_card()."""
      while self.k8s_grid.count():
        item = self.k8s_grid.takeAt(0)
        w = item.widget()
        if w is not None:
          w.deleteLater()
      self._node_cards = {}
      self._card_grid_pos = {}
  
  
  def _current_card_side(self) -> int:
      """Choose a card size that fits the responsive one/two-column grid."""
      width = max(1, self.k8s_grid_container.width())
      self._node_grid_cols = 2 if width >= (self.NODE_CARD_MIN_SIDE * 2 + self.k8s_grid.horizontalSpacing()) else 1
      spacing = self.k8s_grid.horizontalSpacing()
      available = (width - spacing * (self._node_grid_cols - 1)) / self._node_grid_cols
      return int(max(260, min(self.NODE_CARD_MAX_SIDE, available)))
  
  
  def _rescale_node_cards(self):
      """Reflow node cards when the viewport changes, then resize them."""
      previous_cols = getattr(self, "_node_grid_cols", 2)
      side = self._current_card_side()
      if self._node_grid_cols != previous_cols:
        self.k8s_grid.setColumnStretch(0, 1)
        self.k8s_grid.setColumnStretch(self._node_grid_cols + 1, 1)
        self._reflow_node_grid(list(self._node_cards))
      for card in self._node_cards.values():
        card.set_side(side)
  
  
  def _add_node_card(self, node_name: str, is_bucket: bool = False) -> NodeCard:
      """Create a NodeCard for *node_name*, place it at the next free
      grid slot (3 cards per row), size it to fill the row, and track it."""
      card = NodeCard(node_name, is_bucket=is_bucket)
      card.set_side(self._current_card_side())
      card.doubleClicked.connect(self._on_node_double_clicked)
      idx = len(self._node_cards)
      row, col = divmod(idx, self._node_grid_cols)
      self.k8s_grid.addWidget(card, row, col + 1)
      self._node_cards[node_name] = card
      self._card_grid_pos[node_name] = (row, col)
      return card
  
  
  def _get_or_create_node_card(self, node_name: str, is_bucket: bool = False) -> NodeCard:
      """Like _add_node_card(), but reuses the existing card for
      *node_name* if the grid already has one instead of always building
      a fresh one.
  
      This matters because CircularProgress only skips its fill
      animation when setValue() is called again on the *same* ring
      instance with an unchanged value (see progress_ring.py). A brand
      new CircularProgress always starts at 0%, so recreating every
      NodeCard (and therefore every ring) on each refresh made the CPU/
      Memory rings replay their 0%→value animation every single refresh
      cycle — even when the underlying reading hadn't moved at all.
      Reusing cards by node name lets that existing guard actually do
      its job.
      """
      card = self._node_cards.get(node_name)
      if card is not None:
        return card
      return self._add_node_card(node_name, is_bucket=is_bucket)
  
  
  def _reflow_node_grid(self, order: list):
      """Reconcile self._node_cards with *order* (the list of node/
      bucket names that should be shown this refresh, in display order).
  
      Cards for names no longer present are removed and deleted; cards
      for names still present are repositioned (if needed) rather than
      recreated, so widgets — and their live CircularProgress rings —
      persist across refreshes instead of being torn down every time.
      """
      order_set = set(order)
  
      # Drop cards for nodes/buckets that disappeared.
      for name in list(self._node_cards.keys()):
        if name not in order_set:
          card = self._node_cards.pop(name)
          self._card_grid_pos.pop(name, None)
          self.k8s_grid.removeWidget(card)
          card.deleteLater()
  
      for idx, name in enumerate(order):
        card = self._node_cards.get(name)
        if card is None:
          continue
        row, col = divmod(idx, self._node_grid_cols)
        if self._card_grid_pos.get(name) == (row, col):
          continue # already sitting in the right cell — leave it alone
        self.k8s_grid.removeWidget(card)
        self.k8s_grid.addWidget(card, row, col + 1)
        self._card_grid_pos[name] = (row, col)
  
  
  def _style_process_card(self):
      """(Re)apply the Live Processes card colors from the active theme.

      Kept separate from _build_ui so a theme change can re-run it — the tiles,
      labels and process table used to bake their colors at build time and
      kept the startup theme after switching."""
      self.process_interval_lbl.setStyleSheet(
        f"color: {_dashboard_text('muted')}; font-size: 10px; font-weight: 700;"
      )
      # The status line is recolored (green/amber) by every live refresh; only
      # the idle placeholder keeps a muted color that needs re-theming.
      if self.process_status.text().startswith("Waiting"):
        self.process_status.setStyleSheet(
          f"color: {_dashboard_text('muted')}; font-size: 11px;")
      self.process_detail_lbl.setStyleSheet(
        f"color: {_dashboard_text('muted')}; font-size: 10px;"
      )
      for tile, value, caption in self._process_tiles:
        tile.setStyleSheet(
          f"QFrame#process_metric_tile {{ background: {T['BG_DARK']}; "
          f"border: 1px solid {T['BORDER']}; border-radius: 7px; }}"
        )
        value.setStyleSheet(
          f"color: {_dashboard_text('primary')}; font-size: 14px; font-weight: 700;"
        )
        caption.setStyleSheet(
          f"color: {_dashboard_text('muted')}; font-size: 9px; font-weight: 600;"
        )
      self.process_tree.setStyleSheet(
        f"QTreeWidget {{ background: {T['BG_DARK']}; color: {_dashboard_text('primary')}; "
        f"border: 1px solid {T['BORDER']}; border-radius: 8px; padding: 2px; "
        f"alternate-background-color: {T.get('BG_ITEM', T['BG_DARK'])}; }} "
        f"QTreeWidget::item {{ padding: 3px 2px; }} "
        f"QHeaderView::section {{ background: {T['BG_PANEL']}; "
        f"color: {_dashboard_text('muted')}; border: 0; border-bottom: 1px solid {T['BORDER']}; "
        f"padding: 6px 5px; font-size: 10px; font-weight: 700; }}"
      )


  def _apply_styles(self):
      self.ctrl_bar.setStyleSheet(f"QWidget#dash_ctrl {{ background: {T['BG_DARK']}; }}")
      self.page_title_lbl.setStyleSheet(
        f"color: {T['TEXT_PRIMARY']}; font-size: 22px; font-weight: 700;")
      self.page_sub_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 11px;")
      for frame in (self.vm_card["frame"], self.k8s_summary_card["frame"],
              self.workloads_card["frame"], self.services_card["frame"],
              self.events_card["frame"], self.k8s_card["frame"], self.process_card["frame"],
              self.app_connection_card["frame"], self.ssh_pool_card["frame"],
              self.service_activity_card["frame"]):
        frame.setStyleSheet(
          f"QFrame#dash_card {{ background: {T['BG_PANEL']}; "
          f"border: 1px solid {T['BORDER']}; border-radius: 8px; }}"
        )
      self.disk_tree.setStyleSheet(f"QTreeWidget {{ font-size: 12px; }}")
      # Card headers and a few static labels bake their colors into inline
      # QSS at build time, so they must be re-applied on every theme change.
      for card in getattr(self, "_dash_cards", []):
        card["title_lbl"].setStyleSheet(
          f"color: {_dashboard_text('primary')}; font-size: 13px; font-weight: 700;")
      for lbl in (self.updated_lbl, self.disconnected_lbl, self.history_status):
        base = "padding: 40px; font-size: 13px;" if lbl is self.disconnected_lbl else "font-size: 12px;"
        lbl.setStyleSheet(f"color: {_dashboard_text('muted')}; {base}")
      self._style_process_card()
      for tile in self.findChildren(QFrame, "ftp_metric_tile"):
        tile.setStyleSheet(
          f"QFrame#ftp_metric_tile {{ background: {T['BG_PANEL']}; "
          f"border: 1px solid {T['BORDER']}; border-radius: 10px; }}")
      for tile in self.findChildren(QFrame, "app_connection_tile"):
        tile.setStyleSheet(
          f"QFrame#app_connection_tile {{ background: {T['BG_DARK']}; "
          f"border: 1px solid {T['BORDER']}; border-radius: 8px; }}")
      for value, caption, _tile in (
        list(self._app_connection_values.values()) + list(self._ssh_pool_values.values())
      ):
        value.setStyleSheet(
          f"color: {_dashboard_text('primary')}; font-size: 16px; font-weight: 700;"
        )
        caption.setStyleSheet(
          f"color: {_dashboard_text('muted')}; font-size: 10px; font-weight: 600;"
        )
      self.ssh_pool_policy_lbl.setStyleSheet(
        f"color: {_dashboard_text('muted')}; font-size: 11px;"
      )
      for button, selected in (
        (self.machine_view_btn, self.dashboard_pages.currentIndex() == 0),
        (self.app_connections_view_btn, self.dashboard_pages.currentIndex() == 1),
      ):
        button.setStyleSheet(
          f"QPushButton {{ padding: 7px 14px; border: 1px solid {T['BORDER']}; "
          f"border-radius: 7px; color: {_dashboard_text('muted')}; background: {T['BG_PANEL']}; }}"
          f"QPushButton:checked {{ color: {T['TEXT_PRIMARY']}; border-color: {T['ACCENT']}; "
          f"background: {T['BG_HOVER']}; font-weight: 700; }}"
        )
      refresh_theme_icons(self)
      self._update_live_label()
  
  
  def _update_live_label(self):
      if not self.ssh and not self.ftp:
        self.live_lbl.setText("Not connected")
        self.live_lbl.setStyleSheet(f"color: {_dashboard_text('muted')}; font-size: 12px;")
      elif self._active:
        self.live_lbl.setText("Live")
        self.live_lbl.setStyleSheet(f"color: {T['SUCCESS']}; font-size: 12px;")
      else:
        self.live_lbl.setText("⏸ Paused (not on this tab)")
        self.live_lbl.setStyleSheet(f"color: {T['WARNING']}; font-size: 12px;")
  
  
  def _show_disconnected(self):
      self.disconnected_lbl.show()
      self.vm_card["frame"].hide()
      self.k8s_summary_card["frame"].hide()
      self.workloads_card["frame"].hide()
      self.services_card["frame"].hide()
      self.events_card["frame"].hide()
      self.history_card["frame"].hide()
      self.k8s_card["frame"].hide()
      self.ftp_card["frame"].hide()
      self.updated_lbl.setText("")
      self._update_live_label()
  
  
  def _show_connected_placeholder(self):
      self.disconnected_lbl.hide()
      self.vm_card["frame"].show()
      self.process_card["frame"].show()
      self.k8s_summary_card["frame"].show()
      self.workloads_card["frame"].show()
      self.services_card["frame"].show()
      self.events_card["frame"].show()
      self.history_card["frame"].show()
      self.k8s_card["frame"].show()
      self.ftp_card["frame"].hide()
      self._update_live_label()