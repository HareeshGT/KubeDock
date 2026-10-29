"""Compact UI for safety-first AI infrastructure utilities."""
import json
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

import ai_intelligence


class AIIntelligenceDialog(QDialog):
    """Human-reviewed advisor tools; no action in this dialog mutates a cluster."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("KubeDock AI Intelligence")
        self.resize(780, 620)
        root = QVBoxLayout(self)
        intro = QLabel(
            "Incident evidence, comparison, watch planning and remediation validation. "
            "This workspace prepares recommendations only; it does not execute Kubernetes changes."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._build_rightsizing_tab()
        self._build_compare_tab()
        self._build_watch_tab()
        self._build_remediation_tab()
        self._build_services_tab()
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        root.addWidget(close)

    def _output(self, parent_layout):
        view = QTextEdit()
        view.setReadOnly(True)
        view.setLineWrapMode(QTextEdit.WidgetWidth)
        parent_layout.addWidget(view, 1)
        return view

    def _show_result(self, output, result):
        output.setPlainText(json.dumps(result, indent=2, ensure_ascii=False))

    def _build_rightsizing_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        self.cpu_usage = QDoubleSpinBox()
        self.cpu_usage.setRange(0, 10000000)
        self.cpu_usage.setDecimals(2)
        self.cpu_usage.setSuffix(" mCPU")
        self.memory_usage = QDoubleSpinBox()
        self.memory_usage.setRange(0, 1e15)
        self.memory_usage.setDecimals(0)
        self.memory_usage.setSuffix(" bytes")
        self.sample_count = QSpinBox()
        self.sample_count.setRange(0, 1000000)
        self.sample_count.setValue(1)
        self.cpu_request = QDoubleSpinBox()
        self.cpu_request.setRange(0, 10000000)
        self.cpu_request.setSuffix(" mCPU")
        self.memory_request = QDoubleSpinBox()
        self.memory_request.setRange(0, 1e15)
        self.memory_request.setDecimals(0)
        self.memory_request.setSuffix(" bytes")
        form.addRow("Observed CPU usage", self.cpu_usage)
        form.addRow("Observed memory usage", self.memory_usage)
        form.addRow("Number of samples", self.sample_count)
        form.addRow("Current CPU request (optional)", self.cpu_request)
        form.addRow("Current memory request (optional)", self.memory_request)
        layout.addLayout(form)
        run = QPushButton("Calculate conservative recommendation")
        run.clicked.connect(self._recommend_resources)
        layout.addWidget(run)
        layout.addWidget(QLabel("Requires at least five samples. Recommendations never apply themselves."))
        self.rightsizing_output = self._output(layout)
        self.tabs.addTab(tab, "Right-sizing")

    def _recommend_resources(self):
        result = ai_intelligence.recommend_rightsizing(
            cpu_usage_millicores=self.cpu_usage.value(),
            memory_usage_bytes=int(self.memory_usage.value()),
            current_cpu_request_millicores=self.cpu_request.value() or None,
            current_memory_request_bytes=int(self.memory_request.value()) or None,
            sample_count=self.sample_count.value(),
        )
        self._show_result(self.rightsizing_output, result)

    def _build_compare_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        row = QHBoxLayout()
        self.left_snapshot = QLineEdit()
        self.left_snapshot.setPlaceholderText("First cluster snapshot JSON")
        self.right_snapshot = QLineEdit()
        self.right_snapshot.setPlaceholderText("Second cluster snapshot JSON")
        for field, label in ((self.left_snapshot, "Choose left"), (self.right_snapshot, "Choose right")):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, target=field: self._choose_json(target))
            row.addWidget(button)
            row.addWidget(field, 1)
        layout.addLayout(row)
        compare = QPushButton("Compare snapshots")
        compare.clicked.connect(self._compare_snapshots)
        layout.addWidget(compare)
        self.compare_output = self._output(layout)
        layout.addWidget(QLabel('Expected JSON shape: {"cluster":"dev","resources":[{"kind":"Pod","namespace":"apps","name":"api","phase":"Running"}]}'))
        self.tabs.addTab(tab, "Cluster comparison")

    def _choose_json(self, field):
        path, _ = QFileDialog.getOpenFileName(self, "Choose JSON snapshot", "", "JSON files (*.json)")
        if path:
            field.setText(path)

    def _read_json_file(self, path):
        if not path or not Path(path).is_file():
            raise ValueError("Choose an existing JSON file.")
        if Path(path).stat().st_size > 5 * 1024 * 1024:
            raise ValueError("JSON file exceeds the 5 MiB safety limit.")
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("The JSON root must be an object.")
        return value

    def _compare_snapshots(self):
        try:
            result = ai_intelligence.compare_cluster_snapshots(
                self._read_json_file(self.left_snapshot.text()),
                self._read_json_file(self.right_snapshot.text()),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Cannot compare snapshots", str(exc))
            return
        self._show_result(self.compare_output, result)

    def _build_watch_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        self.watch_duration = QSpinBox()
        self.watch_duration.setRange(1, ai_intelligence.MAX_WATCH_SECONDS)
        self.watch_duration.setValue(300)
        self.watch_duration.setSuffix(" seconds")
        self.watch_interval = QSpinBox()
        self.watch_interval.setRange(ai_intelligence.MIN_WATCH_INTERVAL_SECONDS, ai_intelligence.MAX_WATCH_INTERVAL_SECONDS)
        self.watch_interval.setValue(30)
        self.watch_interval.setSuffix(" seconds")
        self.watch_max = QSpinBox()
        self.watch_max.setRange(1, 500)
        self.watch_max.setValue(120)
        form.addRow("Watch duration", self.watch_duration)
        form.addRow("Polling interval", self.watch_interval)
        form.addRow("Maximum observations", self.watch_max)
        layout.addLayout(form)
        layout.addWidget(QLabel("This validates a bounded watch plan only. Live polling must be started explicitly by a future/connected collector; no background loop is started here."))
        button = QPushButton("Validate watch plan")
        button.clicked.connect(self._validate_watch)
        layout.addWidget(button)
        self.watch_output = self._output(layout)
        self.tabs.addTab(tab, "Watch mode")

    def _validate_watch(self):
        try:
            result = ai_intelligence.bounded_watch_config(
                duration_seconds=self.watch_duration.value(),
                interval_seconds=self.watch_interval.value(),
                max_observations=self.watch_max.value(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid watch plan", str(exc))
            return
        self._show_result(self.watch_output, result)

    def _build_remediation_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        form = QFormLayout()
        self.remediation_action = QComboBox()
        self.remediation_action.addItems(["scale", "restart", "rollout_restart", "rollback", "delete"])
        self.remediation_namespace = QLineEdit()
        self.remediation_resource = QComboBox()
        self.remediation_resource.addItems(["deployment", "statefulset", "daemonset", "pod", "job"])
        self.remediation_name = QLineEdit()
        self.remediation_replicas = QSpinBox()
        self.remediation_replicas.setRange(0, 1000)
        self.remediation_replicas.setValue(1)
        self.delete_confirm = QCheckBox("I explicitly confirm proposing a delete operation")
        form.addRow("Action", self.remediation_action)
        form.addRow("Namespace", self.remediation_namespace)
        form.addRow("Resource type", self.remediation_resource)
        form.addRow("Resource name", self.remediation_name)
        form.addRow("Replicas (scale only)", self.remediation_replicas)
        form.addRow("", self.delete_confirm)
        layout.addLayout(form)
        button = QPushButton("Validate remediation proposal")
        button.clicked.connect(self._validate_remediation)
        layout.addWidget(button)
        layout.addWidget(QLabel("The result is a proposal only. Existing confirmation, RBAC, protected-namespace and post-action verification checks remain mandatory."))
        self.remediation_output = self._output(layout)
        self.tabs.addTab(tab, "Remediation planner")

    def _validate_remediation(self):
        plan = {
            "action": self.remediation_action.currentText(),
            "namespace": self.remediation_namespace.text().strip(),
            "resource": self.remediation_resource.currentText(),
            "name": self.remediation_name.text().strip(),
            "replicas": self.remediation_replicas.value(),
            "explicit_delete_confirmation": self.delete_confirm.isChecked(),
        }
        try:
            result = ai_intelligence.validate_remediation_plan(plan)
        except ValueError as exc:
            QMessageBox.warning(self, "Remediation proposal rejected", str(exc))
            return
        self._show_result(self.remediation_output, result)

    def _build_services_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.services_path = QLineEdit()
        self.services_path.setPlaceholderText("JSON file with a services array")
        choose = QPushButton("Choose services JSON")
        choose.clicked.connect(lambda: self._choose_json(self.services_path))
        layout.addWidget(self.services_path)
        layout.addWidget(choose)
        layout.addWidget(QLabel("Build a searchable, deduplicated service index from an exported inventory."))
        button = QPushButton("Build service index")
        button.clicked.connect(self._build_service_index)
        layout.addWidget(button)
        self.services_output = self._output(layout)
        self.tabs.addTab(tab, "Service index")

    def _build_service_index(self):
        try:
            payload = self._read_json_file(self.services_path.text())
            services = payload.get("services")
            if not isinstance(services, list):
                raise ValueError("The JSON object must contain a services array.")
            result = ai_intelligence.build_service_index(services)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Cannot build service index", str(exc))
            return
        self._show_result(self.services_output, {"count": len(result), "services": result})
