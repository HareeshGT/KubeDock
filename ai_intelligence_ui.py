"""Compact UI for safety-first AI infrastructure utilities."""
import json
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
import urllib.error
import urllib.request

from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPushButton, QSpinBox, QTabWidget, QTextEdit, QVBoxLayout,
    QWidget, QListWidget, QListWidgetItem, QSystemTrayIcon,
)

import ai_incident_workflow
import ai_intelligence
import db
import ai_assist
from themes import load_settings, save_settings
from workers import CommandWorker


class PrometheusHistoryWorker(QThread):
    result_ready = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, base_url, namespace, pod, days, parent=None):
        super().__init__(parent)
        self.base_url = base_url
        self.namespace = namespace
        self.pod = pod
        self.days = days

    def run(self):
        try:
            queries = ai_incident_workflow.build_prometheus_history_queries(
                self.namespace, self.pod, self.days
            )
            responses = {}
            for key, query in queries.items():
                url = ai_incident_workflow.prometheus_query_url(self.base_url, query)
                request = urllib.request.Request(
                    url, headers={"Accept": "application/json", "User-Agent": "KubeDock/AI-Workspace"}
                )
                with urllib.request.urlopen(request, timeout=15) as response:
                    body = response.read(2 * 1024 * 1024 + 1)
                if len(body) > 2 * 1024 * 1024:
                    raise ValueError("Prometheus response exceeded the 2 MiB limit.")
                responses[key] = json.loads(body.decode("utf-8"))
            self.result_ready.emit(ai_incident_workflow.parse_prometheus_history_results(responses))
        except Exception as exc:
            self.error.emit(ai_intelligence.redact_evidence(str(exc))[:1000])


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
        self._rightsizing_worker = None
        self._prometheus_worker = None
        self._live_incident_worker = None
        self._pending_live_analysis = False
        self._active_incident_bundle = None
        self._runbook_docs = []
        self._runbook_folder = str(load_settings().get("ai_runbook_directory", "") or "")
        self._validated_remediation_plan = None
        self._last_watch_observation = None
        self._watch_tray = None
        self._build_rightsizing_tab()
        self._build_compare_tab()
        self._watch_timer = QTimer(self)
        self._watch_timer.timeout.connect(self._watch_tick)
        self._watch_worker = None
        self._watch_active = False
        self._watch_deadline = 0.0
        self._watch_observations = 0
        self._watch_config = None
        self._cluster_snapshots = {}
        self._capture_workers = {}
        self._service_records = []
        self._service_capture_worker = None
        self._build_watch_tab()
        self._build_remediation_tab()
        self._incident_worker = None
        self._build_incident_tab()
        self._build_services_tab()
        self._build_cross_system_tab()
        self._build_usage_tab()
        self._build_runbooks_tab()
        self._build_overview_tab()
        self.tabs.insertTab(0, self._overview_tab, "Overview")
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
        layout.setSpacing(10)

        live_group = QGroupBox("Live metrics over SSH")
        live_form = QFormLayout(live_group)
        self.rightsizing_context = QLineEdit()
        self.rightsizing_context.setPlaceholderText("kubectl context name")
        self.rightsizing_namespace = QLineEdit()
        self.rightsizing_namespace.setPlaceholderText("Namespace")
        self.rightsizing_pod = QLineEdit()
        self.rightsizing_pod.setPlaceholderText("Pod name")
        live_form.addRow("Context", self.rightsizing_context)
        live_form.addRow("Namespace", self.rightsizing_namespace)
        live_form.addRow("Pod", self.rightsizing_pod)
        layout.addWidget(live_group)

        history_group = QGroupBox("Historical Prometheus (optional)")
        history_form = QFormLayout(history_group)
        self.prometheus_url = QLineEdit(
            str(load_settings().get("prometheus_history_url", "") or "")
        )
        self.prometheus_url.setPlaceholderText("https://metrics.example.com or http://localhost:9090")
        self.prometheus_days = QSpinBox()
        self.prometheus_days.setRange(1, 30)
        self.prometheus_days.setValue(7)
        self.prometheus_days.setSuffix(" days")
        history_form.addRow("Prometheus URL", self.prometheus_url)
        history_form.addRow("Historical window", self.prometheus_days)
        history_note = QLabel(
            "Uses historical P95 CPU and memory measurements. Remote endpoints must use HTTPS; "
            "plain HTTP is allowed only for localhost. No credentials are embedded in the URL."
        )
        history_note.setWordWrap(True)
        history_group.layout().addWidget(history_note)
        self.prometheus_history_btn = QPushButton("Collect historical P95")
        self.prometheus_history_btn.clicked.connect(self._collect_prometheus_history)
        history_group.layout().addWidget(self.prometheus_history_btn)
        layout.addWidget(history_group)

        usage_group = QGroupBox("Recommendation inputs")
        form = QFormLayout(usage_group)
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
        layout.addWidget(usage_group)

        actions = QHBoxLayout()
        collect = QPushButton("Collect 5 live samples over SSH")
        collect.clicked.connect(self._collect_rightsizing_metrics)
        actions.addWidget(collect)
        run = QPushButton("Calculate conservative recommendation")
        run.clicked.connect(self._recommend_resources)
        actions.addWidget(run)
        layout.addLayout(actions)
        layout.addWidget(QLabel(
            "At least five valid measurements are required. Prometheus supplies historical P95; "
            "SSH sampling uses peak observed usage. Recommendations never apply themselves."
        ))
        self.rightsizing_output = self._output(layout)
        self.tabs.addTab(tab, "Right-sizing")

    def _collect_prometheus_history(self):
        if self._prometheus_worker is not None:
            return
        endpoint = self.prometheus_url.text().strip()
        namespace = self.rightsizing_namespace.text().strip()
        pod = self.rightsizing_pod.text().strip()
        try:
            ai_incident_workflow.validate_prometheus_base_url(endpoint)
            ai_incident_workflow.build_prometheus_history_queries(
                namespace, pod, self.prometheus_days.value()
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Prometheus target", str(exc))
            return
        save_settings(prometheus_history_url=endpoint)
        self.prometheus_history_btn.setEnabled(False)
        self.rightsizing_output.setPlainText(
            "Querying historical P95 CPU and memory metrics from Prometheus…"
        )
        worker = PrometheusHistoryWorker(
            endpoint, namespace, pod, self.prometheus_days.value(), parent=self
        )
        self._prometheus_worker = worker
        worker.result_ready.connect(self._on_prometheus_history)
        worker.error.connect(self._on_prometheus_history_error)
        worker.finished.connect(self._on_prometheus_history_finished)
        worker.start()

    def _on_prometheus_history(self, result):
        self.cpu_usage.setValue(result["cpu_usage_millicores"])
        self.memory_usage.setValue(result["memory_usage_bytes"])
        self.sample_count.setValue(min(1000000, result["sample_count"]))
        self.rightsizing_output.setPlainText(
            "Historical Prometheus P95 metrics loaded for the selected window. "
            f"Valid minimum sample count: {result['sample_count']}. "
            "Current resource requests are unchanged; run the recommendation explicitly to calculate targets."
        )

    def _on_prometheus_history_error(self, message):
        self.rightsizing_output.setPlainText(
            "Prometheus history unavailable. No estimates were substituted.\n"
            + ai_intelligence.redact_evidence(message)
        )

    def _on_prometheus_history_finished(self):
        self._prometheus_worker = None
        self.prometheus_history_btn.setEnabled(True)

    def _collect_rightsizing_metrics(self):
        if self._rightsizing_worker is not None:
            return
        ssh = self._find_ssh_connection()
        context = self.rightsizing_context.text().strip()
        namespace = self.rightsizing_namespace.text().strip()
        pod = self.rightsizing_pod.text().strip()
        if ssh is None:
            QMessageBox.warning(self, "SSH not connected", "Connect to a VM in KubeDock before collecting metrics.")
            return
        if not context or not namespace or not pod:
            QMessageBox.information(self, "Target required", "Enter the kubectl context, namespace, and pod name.")
            return
        context_q, namespace_q, pod_q = map(shlex.quote, (context, namespace, pod))
        sample_command = (
            "for i in 1 2 3 4 5; do printf '__SAMPLE_%s__\\n' \"$i\"; "
            + "kubectl --context " + context_q + " -n " + namespace_q
            + " top pod " + pod_q + " --containers --no-headers 2>&1; sleep 5; done"
        )
        request_expr = r'{range .spec.containers[*]}CPU={.resources.requests.cpu};MEM={.resources.requests.memory}{"\n"}{end}'
        request_command = (
            "printf '\\n__REQUESTS__\\n'; kubectl --context " + context_q
            + " -n " + namespace_q + " get pod " + pod_q
            + " -o jsonpath=" + shlex.quote(request_expr) + " 2>&1"
        )
        inner = sample_command + "; " + request_command
        worker = CommandWorker(ssh, "bash -lc " + shlex.quote(inner), timeout=45)
        self._rightsizing_worker = worker
        worker.result.connect(self._on_rightsizing_metrics)
        worker.error.connect(lambda error: self.rightsizing_output.setPlainText("Metric collection failed: " + ai_intelligence.redact_evidence(error)))
        worker.finished.connect(self._on_rightsizing_metrics_finished)
        self.rightsizing_output.setPlainText("Collecting five CPU/memory samples (about 25 seconds)…")
        worker.start()

    def _on_rightsizing_metrics(self, output, error, exit_code):
        if exit_code != 0:
            self.rightsizing_output.setPlainText(
                "Metric collection failed; values remain unchanged.\\n"
                + ai_intelligence.redact_evidence(error or output or str(exit_code))
            )
            return
        samples = {}
        requests_text = []
        current_sample = None
        in_requests = False
        for line in (output or "").splitlines():
            match = re.match(r"__SAMPLE_(\d+)__", line.strip())
            if match:
                current_sample = int(match.group(1))
                samples.setdefault(current_sample, [])
                in_requests = False
                continue
            if line.strip() == "__REQUESTS__":
                in_requests = True
                current_sample = None
                continue
            if in_requests:
                requests_text.append(line)
            elif current_sample is not None:
                samples[current_sample].append(line)
        cpu_samples = []
        memory_samples = []
        for number in sorted(samples):
            cpu_total = 0.0
            memory_total = 0
            found = False
            for line in samples[number]:
                fields = line.split()
                if len(fields) < 3:
                    continue
                try:
                    cpu_total += ai_intelligence.parse_cpu_millicores(fields[-2])
                    memory_total += ai_intelligence.parse_memory_bytes(fields[-1])
                    found = True
                except ValueError:
                    continue
            if found:
                cpu_samples.append(cpu_total)
                memory_samples.append(memory_total)
        if len(cpu_samples) < 5 or len(memory_samples) < 5:
            self.rightsizing_output.setPlainText(
                "Could not collect five valid metrics samples. Check Metrics Server/RBAC and retry.\\n"
                + ai_intelligence.redact_evidence(output or error or "")
            )
            return
        cpu_request = 0.0
        memory_request = 0
        request_count = 0
        for line in requests_text:
            if not line.strip() or line.strip().lower().startswith("error"):
                continue
            fields = dict(
                part.split("=", 1) for part in line.split(";") if "=" in part
            )
            try:
                cpu_text = fields.get("CPU", "").strip()
                memory_text = fields.get("MEM", "").strip()
                if cpu_text:
                    cpu_request += ai_intelligence.parse_cpu_millicores(cpu_text)
                if memory_text:
                    memory_request += ai_intelligence.parse_memory_bytes(memory_text)
                if cpu_text or memory_text:
                    request_count += 1
            except ValueError:
                continue
        self.cpu_usage.setValue(max(cpu_samples))
        self.memory_usage.setValue(max(memory_samples))
        self.sample_count.setValue(min(len(cpu_samples), len(memory_samples)))
        self.cpu_request.setValue(cpu_request if request_count else 0)
        self.memory_request.setValue(memory_request if request_count else 0)
        self.rightsizing_output.setPlainText(
            "Collected " + str(min(len(cpu_samples), len(memory_samples)))
            + " valid samples. Usage fields contain the peak observed across samples; resource requests "
            + ("were read from the pod spec." if request_count else "were not available in the pod spec.")
        )

    def _on_rightsizing_metrics_finished(self):
        self._rightsizing_worker = None

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
        form = QFormLayout()
        self.left_cluster_name = QLineEdit("cluster-a")
        self.left_cluster_context = QLineEdit()
        self.left_cluster_context.setPlaceholderText("kubectl context name")
        self.right_cluster_name = QLineEdit("cluster-b")
        self.right_cluster_context = QLineEdit()
        self.right_cluster_context.setPlaceholderText("kubectl context name")
        form.addRow("Left cluster label", self.left_cluster_name)
        form.addRow("Left kubectl context", self.left_cluster_context)
        form.addRow("Right cluster label", self.right_cluster_name)
        form.addRow("Right kubectl context", self.right_cluster_context)
        layout.addLayout(form)
        actions = QHBoxLayout()
        left_capture = QPushButton("Capture left cluster")
        left_capture.clicked.connect(lambda: self._capture_cluster_snapshot("left"))
        right_capture = QPushButton("Capture right cluster")
        right_capture.clicked.connect(lambda: self._capture_cluster_snapshot("right"))
        actions.addWidget(left_capture)
        actions.addWidget(right_capture)
        layout.addLayout(actions)
        compare_live = QPushButton("Compare captured clusters")
        compare_live.clicked.connect(self._compare_captured_snapshots)
        layout.addWidget(compare_live)
        layout.addWidget(QLabel("Capture uses read-only kubectl inventory queries over the existing SSH connection. It omits Secret values and compares normalized resource status/configuration only."))
        self.compare_output = self._output(layout)
        layout.addWidget(QLabel("Alternatively compare exported JSON snapshots:"))
        row = QHBoxLayout()
        self.left_snapshot = QLineEdit()
        self.left_snapshot.setPlaceholderText("First snapshot JSON")
        self.right_snapshot = QLineEdit()
        self.right_snapshot.setPlaceholderText("Second snapshot JSON")
        for field, label in ((self.left_snapshot, "Choose left"), (self.right_snapshot, "Choose right")):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, target=field: self._choose_json(target))
            row.addWidget(button)
            row.addWidget(field, 1)
        layout.addLayout(row)
        compare = QPushButton("Compare JSON files")
        compare.clicked.connect(self._compare_snapshots)
        layout.addWidget(compare)
        layout.addWidget(QLabel('Expected JSON shape: {"cluster":"dev","resources":[{"kind":"Pod","namespace":"apps","name":"api","phase":"Running"}]}'))
        self.tabs.addTab(tab, "Cluster comparison")

    def _capture_cluster_snapshot(self, side):
        if side in self._capture_workers:
            return
        ssh = self._find_ssh_connection()
        if ssh is None:
            QMessageBox.warning(self, "SSH not connected", "Connect to a VM in KubeDock before capturing cluster snapshots.")
            return
        context_field = self.left_cluster_context if side == "left" else self.right_cluster_context
        label_field = self.left_cluster_name if side == "left" else self.right_cluster_name
        context = context_field.text().strip()
        if not context:
            QMessageBox.information(self, "Context required", f"Enter the kubectl context for the {side} cluster.")
            return
        inner = "kubectl --context " + shlex.quote(context) + " get pods,deployments,services,statefulsets,daemonsets,jobs -A -o json 2>&1"
        worker = CommandWorker(ssh, "bash -lc " + shlex.quote(inner), timeout=45)
        self._capture_workers[side] = worker
        worker.result.connect(lambda output, error, code, selected=side, label=label_field.text().strip(), ctx=context: self._on_cluster_snapshot(selected, label, ctx, output, error, code))
        worker.error.connect(lambda error, selected=side: self.compare_output.append("Capture error (" + selected + "): " + ai_intelligence.redact_evidence(error)))
        worker.finished.connect(lambda selected=side: self._capture_workers.pop(selected, None))
        worker.start()

    def _on_cluster_snapshot(self, side, label, context, output, error, exit_code):
        if exit_code != 0:
            QMessageBox.warning(self, "Cluster capture failed", ai_intelligence.redact_evidence(error or output or f"kubectl exited with {exit_code}"))
            return
        try:
            payload = json.loads(output)
            items = payload.get("items")
            if not isinstance(items, list):
                raise ValueError("kubectl did not return a Kubernetes resource list.")
            resources = []
            for item in items[:5000]:
                if not isinstance(item, dict):
                    continue
                metadata = item.get("metadata") or {}
                spec = item.get("spec") or {}
                status = item.get("status") or {}
                if not isinstance(status, dict):
                    status = {}
                pod_spec = spec.get("template", {}).get("spec", {}) if isinstance(spec.get("template"), dict) else spec
                containers = pod_spec.get("containers", []) if isinstance(pod_spec, dict) else []
                container_statuses = status.get("containerStatuses", [])
                normalized = {
                    "kind": str(item.get("kind") or "").lower(),
                    "namespace": str(metadata.get("namespace") or "default"),
                    "name": str(metadata.get("name") or ""),
                    "phase": status.get("phase"),
                    "replicas": spec.get("replicas"),
                    "ready_replicas": status.get("readyReplicas"),
                    "images": sorted(str(c.get("image")) for c in containers if isinstance(c, dict) and c.get("image")),
                    "ports": sorted(str(p.get("port")) for p in (spec.get("ports") or []) if isinstance(p, dict) and p.get("port")),
                    "restarts": sum(int(c.get("restartCount") or 0) for c in container_statuses if isinstance(c, dict)),
                }
                if normalized["kind"] and normalized["name"]:
                    resources.append(normalized)
            self._cluster_snapshots[side] = {"cluster": label or context, "context": context, "resources": resources}
            self.compare_output.append(f"Captured {side} cluster {label or context}: {len(resources)} resource(s).")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Invalid cluster snapshot", str(exc))

    def _compare_captured_snapshots(self):
        if "left" not in self._cluster_snapshots or "right" not in self._cluster_snapshots:
            QMessageBox.information(self, "Two snapshots required", "Capture both left and right cluster snapshots first.")
            return
        result = ai_intelligence.compare_cluster_snapshots(
            self._cluster_snapshots["left"], self._cluster_snapshots["right"]
        )
        self._show_result(self.compare_output, result)

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
        self.watch_namespace = QLineEdit()
        self.watch_namespace.setPlaceholderText("e.g. production")
        self.watch_context = QLineEdit()
        self.watch_context.setPlaceholderText("Optional kubectl context")
        self.watch_resource = QComboBox()
        self.watch_resource.addItems(["pod", "deployment", "statefulset", "daemonset", "service", "job"])
        self.watch_name = QLineEdit()
        self.watch_name.setPlaceholderText("Resource name")
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
        form.addRow("Namespace", self.watch_namespace)
        form.addRow("Context (optional)", self.watch_context)
        form.addRow("Resource kind", self.watch_resource)
        form.addRow("Resource name", self.watch_name)
        form.addRow("Watch duration", self.watch_duration)
        form.addRow("Polling interval", self.watch_interval)
        form.addRow("Maximum observations", self.watch_max)
        layout.addLayout(form)
        layout.addWidget(QLabel(
            "Starts an explicit, bounded, read-only kubectl get watch over the current SSH session. "
            "No automated remediation is performed. Requires an active KubeDock SSH connection."
        ))
        actions = QHBoxLayout()
        self.watch_start_btn = QPushButton("Start Watch")
        self.watch_start_btn.clicked.connect(self._start_watch)
        self.watch_stop_btn = QPushButton("Stop Watch")
        self.watch_stop_btn.setEnabled(False)
        self.watch_stop_btn.clicked.connect(self._stop_watch)
        self.watch_correlate_btn = QPushButton("Correlate observations")
        self.watch_correlate_btn.clicked.connect(self._correlate_watch_observations)
        actions.addWidget(self.watch_correlate_btn)
        actions.addWidget(self.watch_start_btn)
        actions.addWidget(self.watch_stop_btn)
        layout.addLayout(actions)
        self.watch_output = self._output(layout)
        self.tabs.addTab(tab, "Watch mode")

    def _find_ssh_connection(self):
        node = self.parentWidget()
        while node is not None:
            ssh = getattr(node, "ssh", None)
            if ssh is not None:
                return ssh
            node = node.parentWidget()
        return None

    def _start_watch(self):
        try:
            self._watch_config = ai_intelligence.bounded_watch_config(
                duration_seconds=self.watch_duration.value(),
                interval_seconds=self.watch_interval.value(),
                max_observations=self.watch_max.value(),
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid watch plan", str(exc))
            return
        if not self.watch_namespace.text().strip() or not self.watch_name.text().strip():
            QMessageBox.information(self, "Target required", "Enter the target namespace and resource name.")
            return
        if self._find_ssh_connection() is None:
            QMessageBox.warning(self, "SSH not connected", "Connect to a VM in KubeDock before starting a watch.")
            return
        self._watch_observations = 0
        self._watch_deadline = time.monotonic() + self._watch_config["duration_seconds"]
        self._watch_active = True
        self.watch_output.clear()
        self.watch_start_btn.setEnabled(False)
        self.watch_stop_btn.setEnabled(True)
        self.watch_output.append("Watch started. Read-only polling only; automatic mutations are disabled.")
        self._watch_tick()
        self._watch_timer.start(self._watch_config["interval_seconds"] * 1000)

    def _watch_tick(self):
        if not self._watch_active:
            return
        if time.monotonic() >= self._watch_deadline or self._watch_observations >= self._watch_config["max_observations"]:
            self._stop_watch()
            return
        if self._watch_worker is not None:
            return
        ssh = self._find_ssh_connection()
        if ssh is None:
            self.watch_output.append("SSH connection is no longer available; watch stopped.")
            self._stop_watch()
            return
        namespace = shlex.quote(self.watch_namespace.text().strip())
        name = shlex.quote(self.watch_name.text().strip())
        resource = self.watch_resource.currentText()
        context = self.watch_context.text().strip()
        context_flag = "--context " + shlex.quote(context) + " " if context else ""
        inner = "kubectl " + context_flag + "-n " + namespace + " get " + resource + " " + name + " -o wide 2>&1"
        command = "bash -lc " + shlex.quote(inner)
        worker = CommandWorker(ssh, command, timeout=20)
        self._watch_worker = worker
        worker.result.connect(self._on_watch_result)
        worker.error.connect(self._on_watch_error)
        worker.finished.connect(self._on_watch_finished)
        worker.start()


    def _correlate_watch_observations(self):
        text = self.watch_output.toPlainText().strip()
        if not text:
            QMessageBox.information(self, "No observations", "Start a watch and collect observations first.")
            return
        result = ai_intelligence.correlate_incident_signals(logs=text.splitlines())
        self.watch_output.append("\nCandidate signal correlation (hypotheses, not proof):\n" + json.dumps(result, indent=2, ensure_ascii=False))

    def _on_watch_result(self, output, error, exit_code):
        self._watch_observations += 1
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        text = ai_intelligence.redact_evidence((output or "") + ("\n" + error if error else ""))
        self.watch_output.append("[" + stamp + "] exit=" + str(exit_code) + "\n" + (text or "(no output)"))
        if exit_code != 0:
            self.watch_output.append("Observation failed; treat the resource state as unknown, not healthy.")
        if self._watch_config and self._watch_observations >= self._watch_config["max_observations"]:
            self._stop_watch()

    def _on_watch_error(self, error):
        self.watch_output.append("[watch error] " + ai_intelligence.redact_evidence(error))

    def _on_watch_finished(self):
        self._watch_worker = None

    def _stop_watch(self):
        self._watch_active = False
        self._watch_timer.stop()
        self.watch_start_btn.setEnabled(True)
        self.watch_stop_btn.setEnabled(False)
        self.watch_output.append("Watch stopped after " + str(self._watch_observations) + " observation(s).")

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



    def _build_cross_system_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.cross_system_path = QLineEdit()
        self.cross_system_path.setPlaceholderText("Incident/evidence bundle JSON")
        choose = QPushButton("Choose evidence JSON")
        choose.clicked.connect(lambda: self._choose_json(self.cross_system_path))
        layout.addWidget(self.cross_system_path)
        layout.addWidget(choose)
        layout.addWidget(QLabel(
            "Correlates recognizable failure signals across supplied host, Docker, Kubernetes, "
            "application-log and metrics evidence. Results are hypotheses, not causal proof."
        ))
        analyze = QPushButton("Correlate evidence signals")
        analyze.clicked.connect(self._correlate_evidence_file)
        layout.addWidget(analyze)
        self.cross_system_output = self._output(layout)
        self.tabs.addTab(tab, "Cross-system diagnosis")

    def _correlate_evidence_file(self):
        try:
            payload = self._read_json_file(self.cross_system_path.text())
            evidence = payload.get("evidence", payload)
            if not isinstance(evidence, dict):
                raise ValueError("Evidence must be a JSON object.")
            log_keys = ("logs", "application_logs", "host_logs", "docker_logs", "container_logs", "system_logs")
            logs = []
            events = []
            metrics = {}
            for key, value in evidence.items():
                lowered = str(key).lower()
                if any(token in lowered for token in ("log", "stdout", "stderr")):
                    if isinstance(value, str):
                        logs.extend(value.splitlines())
                    elif isinstance(value, list):
                        logs.extend(str(item) for item in value[:ai_intelligence.MAX_EVIDENCE_ITEMS])
                elif "event" in lowered:
                    events.extend(value if isinstance(value, list) else [value])
                elif "metric" in lowered or "cpu" in lowered or "memory" in lowered:
                    metrics[key] = value
            result = ai_intelligence.correlate_incident_signals(
                logs=logs, events=events, metrics=metrics
            )
            result["sources"] = {
                "cluster": payload.get("cluster", "unknown"),
                "namespace": payload.get("namespace", "unknown"),
                "resource": payload.get("resource", "unknown"),
                "log_sources_detected": [key for key in log_keys if key in evidence],
            }
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Cannot correlate evidence", str(exc))
            return
        self._show_result(self.cross_system_output, result)

    def _build_usage_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.addWidget(QLabel(
            "Opt-in local aggregates only: provider, task profile, call count, error count, "
            "and average latency. Prompts, logs, API keys, and response text are never recorded."
        ))
        refresh = QPushButton("Refresh usage summary")
        refresh.clicked.connect(self._refresh_usage_summary)
        layout.addWidget(refresh)
        self.usage_output = self._output(layout)
        self._refresh_usage_summary()
        self.tabs.addTab(tab, "Usage / latency")

    def _refresh_usage_summary(self):
        enabled = bool((load_settings().get("ai") or {}).get("usage_telemetry_enabled", False))
        if not enabled:
            result = {
                "enabled": False,
                "message": "Telemetry is disabled. Enable the opt-in checkbox in Settings → AI to start collecting local aggregate counters.",
                "records": [],
            }
        else:
            try:
                ledger = ai_intelligence.UsageLedger(str(db.DB_PATH))
                result = {"enabled": True, "records": ledger.summary(30)}
            except Exception as exc:
                result = {"enabled": True, "error": str(exc), "records": []}
        self._show_result(self.usage_output, result)


    def _build_incident_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.incident_bundle_path = QLineEdit()
        self.incident_bundle_path.setPlaceholderText("Redacted incident bundle JSON")
        choose = QPushButton("Choose incident bundle")
        choose.clicked.connect(lambda: self._choose_json(self.incident_bundle_path))
        layout.addWidget(self.incident_bundle_path)
        layout.addWidget(choose)
        layout.addWidget(QLabel(
            "Analyze an exported incident bundle with the configured Deep Diagnosis or Local / Private profile. "
            "Evidence is redacted again before provider submission. No remediation is executed."
        ))
        self.incident_profile_combo = QComboBox()
        self.incident_profile_combo.addItem("Deep Diagnosis", "deep_diagnosis")
        self.incident_profile_combo.addItem("Local / Private (Ollama)", "local_private")
        layout.addWidget(self.incident_profile_combo)
        analyze = QPushButton("Investigate incident with AI")
        analyze.clicked.connect(self._investigate_incident_bundle)
        layout.addWidget(analyze)
        self.incident_output = self._output(layout)
        self.tabs.addTab(tab, "Incident investigator")

    def _investigate_incident_bundle(self):
        try:
            payload = self._read_json_file(self.incident_bundle_path.text())
            evidence = payload.get("evidence", {})
            if not isinstance(evidence, dict):
                raise ValueError("Incident evidence must be a JSON object.")
            logs = evidence.get("logs", "")
            if not isinstance(logs, str):
                logs = json.dumps(logs, ensure_ascii=False)
            resource_context = evidence.get("live_resource_evidence", "")
            if not isinstance(resource_context, str):
                resource_context = json.dumps(resource_context, ensure_ascii=False)
            signals = evidence.get("candidate_signals")
            if signals:
                resource_context += "\n\nPreviously correlated candidate signals (not proof):\n" + json.dumps(signals, ensure_ascii=False)
            if not logs.strip() and not resource_context.strip():
                raise ValueError("The incident bundle contains no logs or resource evidence.")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Cannot investigate incident", str(exc))
            return
        settings = ai_assist.get_ai_settings()
        task_profile = self.incident_profile_combo.currentData() or "deep_diagnosis"
        assignment = settings.get("task_profiles", {}).get(task_profile, {})
        provider = assignment.get("provider") or settings["provider"]
        api_key = ai_assist.get_api_key(provider)
        if not api_key and provider != "ollama":
            QMessageBox.information(self, "API key required", f"Configure an API key for {provider} in Settings → AI.")
            return
        model = assignment.get("model_id") or ai_assist.get_model(provider)
        self.incident_output.setPlainText("Investigating incident evidence…")
        self._incident_worker = ai_assist.AIExplainWorker(
            provider, api_key, model,
            str(payload.get("resource") or "unknown-resource"),
            str(payload.get("namespace") or "unknown"),
            str(evidence.get("container") or "default"),
            logs,
            parent=self,
            resource_context=resource_context,
            task_profile=task_profile,
        )
        self._incident_worker.done.connect(self.incident_output.setPlainText)
        self._incident_worker.error.connect(lambda message: self.incident_output.setPlainText("Investigation failed: " + message))
        self._incident_worker.start()

    def _build_services_tab(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        self.service_context = QLineEdit()
        self.service_context.setPlaceholderText("kubectl context name for live service discovery")
        layout.addWidget(self.service_context)
        capture = QPushButton("Discover services over current SSH")
        capture.clicked.connect(self._capture_services)
        layout.addWidget(capture)
        self.service_filter = QLineEdit()
        self.service_filter.setPlaceholderText("Filter by cluster, namespace, service, or port")
        self.service_filter.textChanged.connect(self._filter_services)
        layout.addWidget(self.service_filter)
        self.service_list = QListWidget()
        self.service_list.itemDoubleClicked.connect(self._show_selected_service)
        layout.addWidget(self.service_list, 1)
        self.services_path = QLineEdit()
        self.services_path.setPlaceholderText("Or load JSON with a services array")
        choose = QPushButton("Choose services JSON")
        choose.clicked.connect(lambda: self._choose_json(self.services_path))
        layout.addWidget(self.services_path)
        layout.addWidget(choose)
        button = QPushButton("Build service index from JSON")
        button.clicked.connect(self._build_service_index)
        layout.addWidget(button)
        layout.addWidget(QLabel("Double-click a service to view its safe identifiers and a read-only kubectl command. No Service exposure or mutation is performed."))
        self.services_output = self._output(layout)
        self.tabs.addTab(tab, "Service index")

    def _capture_services(self):
        if self._service_capture_worker is not None:
            return
        ssh = self._find_ssh_connection()
        context = self.service_context.text().strip()
        if ssh is None:
            QMessageBox.warning(self, "SSH not connected", "Connect to a VM in KubeDock before discovering services.")
            return
        if not context:
            QMessageBox.information(self, "Context required", "Enter the kubectl context for service discovery.")
            return
        inner = "kubectl --context " + shlex.quote(context) + " get services -A -o json 2>&1"
        worker = CommandWorker(ssh, "bash -lc " + shlex.quote(inner), timeout=35)
        self._service_capture_worker = worker
        worker.result.connect(lambda output, error, code, label=context: self._on_services_captured(label, output, error, code))
        worker.error.connect(lambda error: self.services_output.setPlainText("Service discovery failed: " + ai_intelligence.redact_evidence(error)))
        worker.finished.connect(self._on_services_capture_finished)
        worker.start()

    def _on_services_captured(self, context, output, error, exit_code):
        if exit_code != 0:
            self.services_output.setPlainText("Service discovery failed; state is unknown.\n" + ai_intelligence.redact_evidence(error or output))
            return
        try:
            payload = json.loads(output)
            items = payload.get("items")
            if not isinstance(items, list):
                raise ValueError("kubectl did not return a service list.")
            records = []
            for item in items[:5000]:
                if not isinstance(item, dict):
                    continue
                metadata = item.get("metadata") or {}
                spec = item.get("spec") or {}
                records.append({
                    "name": metadata.get("name", ""),
                    "namespace": metadata.get("namespace", "default"),
                    "cluster": context,
                    "ports": [p.get("port") for p in (spec.get("ports") or []) if isinstance(p, dict) and p.get("port") is not None],
                    "labels": metadata.get("labels") or {},
                    "type": spec.get("type", "ClusterIP"),
                })
            self._service_records = ai_intelligence.build_service_index(records)
            self._filter_services()
            self.services_output.setPlainText(f"Discovered {len(self._service_records)} services from context {context}.")
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Invalid service inventory", str(exc))

    def _on_services_capture_finished(self):
        self._service_capture_worker = None

    def _filter_services(self, _query=None):
        if not hasattr(self, "service_list"):
            return
        query = self.service_filter.text().strip().lower()
        self.service_list.clear()
        for service in self._service_records:
            searchable = " ".join((
                service.get("cluster", ""), service.get("namespace", ""),
                service.get("name", ""), " ".join(str(p) for p in service.get("ports", [])),
            )).lower()
            if query and query not in searchable:
                continue
            item = QListWidgetItem(
                service["namespace"] + "/" + service["name"] +
                "  ·  " + str(service.get("cluster") or "cluster") +
                "  ·  ports " + (", ".join(str(p) for p in service.get("ports", [])) or "none")
            )
            item.setData(Qt.UserRole, service)
            self.service_list.addItem(item)

    def _show_selected_service(self, item):
        service = item.data(Qt.UserRole) or {}
        command = "kubectl --context " + shlex.quote(str(service.get("cluster") or "")) + " -n " + shlex.quote(str(service.get("namespace") or "")) + " get service " + shlex.quote(str(service.get("name") or ""))
        self._show_result(self.services_output, {
            "service": service,
            "read_only_inspection_command": command,
            "note": "The command is shown for inspection only and has not been executed.",
        })

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
        self._service_records = result
        self._filter_services()
        self._show_result(self.services_output, {"count": len(result), "services": result})

    def closeEvent(self, event):
        if self._watch_active:
            self._stop_watch()
        super().closeEvent(event)



def open_ai_intelligence_workspace(parent=None):
    """Open the shared advisory workspace from Settings, Ops Mind, or other UI entry points."""
    from PyQt5.QtWidgets import QMessageBox
    try:
        dialog = AIIntelligenceDialog(parent)
        return dialog.exec_()
    except Exception as exc:
        QMessageBox.warning(
            parent,
            "AI Intelligence Workspace Unavailable",
            f"Could not open the workspace: {exc}",
        )
        return None
