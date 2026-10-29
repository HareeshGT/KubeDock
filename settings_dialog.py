"""settings_dialog.py — App-wide Settings dialog."""

from PyQt5.QtCore import QEvent, QTimer, Qt
from PyQt5.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QCheckBox,
    QComboBox,
    QTabBar,
    QTabWidget,
    QWidget,
    QScrollArea,
    QDialogButtonBox,
    QFrame,
    QLineEdit,
    QApplication,
    QListWidget,
    QInputDialog,
    QMessageBox,
)
from PyQt5.QtGui import QFont

from themes import T, apply_qss_to, load_settings, save_settings
import security
import ai_assist
import k8s_ai_ops
from lock_screen import SetPinDialog

_AUTOLOCK_OPTIONS = [
    ("1 minute", 1),
    ("2 minutes", 2),
    ("5 minutes", 5),
    ("10 minutes", 10),
    ("15 minutes", 15),
    ("30 minutes", 30),
]


class SwipeTabWidget(QTabWidget):
    """QTabWidget that supports horizontal swipe gestures in addition to clicks."""

    _DRAG_THRESHOLD = 65
    _WHEEL_THRESHOLD = 55
    _SWIPE_COOLDOWN_MS = 350

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTabBar(QTabBar())
        self.tabBar().setUsesScrollButtons(True)
        self.tabBar().setExpanding(False)

        self._swipe_start = None
        self._swipe_dragging = False
        self._wheel_distance = 0
        self._wheel_locked = False

        self.tabBar().installEventFilter(self)

    def addTab(self, widget, label):
        index = super().addTab(widget, label)
        self._install_swipe_filter(widget)
        return index

    def _install_swipe_filter(self, widget):
        """Watch the tab page and its children so a swipe can start anywhere."""
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def _reset_swipe(self):
        self._swipe_start = None
        self._swipe_dragging = False

    def _change_tab_from_direction(self, direction):
        current = self.currentIndex()
        target = current + direction

        if target < 0 or target >= self.count():
            return False

        self.setCurrentIndex(target)
        return True

    def _unlock_wheel(self):
        self._wheel_locked = False
        self._wheel_distance = 0

    def _handle_horizontal_wheel(self, event):
        pixel_delta = event.pixelDelta().x()
        angle_delta = event.angleDelta().x()
        delta = pixel_delta if pixel_delta else angle_delta

        if not delta:
            return False

        # Ignore predominantly vertical scrolling so normal QScrollArea behavior
        # continues to work inside the settings pages.
        vertical = event.pixelDelta().y() or event.angleDelta().y()
        if abs(delta) <= abs(vertical) * 1.15:
            return False

        if self._wheel_locked:
            return True

        if self._wheel_distance and (delta > 0) != (self._wheel_distance > 0):
            self._wheel_distance = 0

        self._wheel_distance += delta

        if abs(self._wheel_distance) < self._WHEEL_THRESHOLD:
            return True

        direction = -1 if self._wheel_distance > 0 else 1
        changed = self._change_tab_from_direction(direction)
        self._wheel_distance = 0

        if changed:
            self._wheel_locked = True
            QTimer.singleShot(self._SWIPE_COOLDOWN_MS, self._unlock_wheel)

        return True

    def eventFilter(self, watched, event):
        event_type = event.type()

        if event_type == QEvent.Wheel:
            if self._handle_horizontal_wheel(event):
                return True

        elif event_type == QEvent.MouseButtonPress:
            if event.button() == Qt.LeftButton:
                self._swipe_start = event.pos()
                self._swipe_dragging = False

        elif event_type == QEvent.MouseMove:
            if (
                self._swipe_start is not None
                and event.buttons() & Qt.LeftButton
            ):
                dx = event.pos().x() - self._swipe_start.x()
                dy = event.pos().y() - self._swipe_start.y()

                if not self._swipe_dragging:
                    horizontal = abs(dx) > abs(dy) * 1.25
                    if horizontal and abs(dx) >= self._DRAG_THRESHOLD:
                        self._swipe_dragging = True

                if self._swipe_dragging:
                    return True

        elif event_type == QEvent.MouseButtonRelease:
            if event.button() == Qt.LeftButton and self._swipe_start is not None:
                dx = event.pos().x() - self._swipe_start.x()
                dy = event.pos().y() - self._swipe_start.y()

                if (
                    abs(dx) >= self._DRAG_THRESHOLD
                    and abs(dx) > abs(dy) * 1.25
                ):
                    # Dragging left moves to the next tab; dragging right moves
                    # to the previous tab.
                    self._change_tab_from_direction(-1 if dx > 0 else 1)
                    self._reset_swipe()
                    return True

                self._reset_swipe()

        return super().eventFilter(watched, event)



class CustomModelManagerDialog(QDialog):
    """Edit the selected provider's user-defined models without changing built-ins."""

    def __init__(self, provider_id, models, parent=None):
        super().__init__(parent)
        self.provider_id = provider_id
        self._models = [dict(item) for item in (models or [])]
        self.setWindowTitle("Manage Custom AI Models")
        self.resize(520, 360)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"Custom models for {ai_assist.PROVIDERS[provider_id]['label']}. "
            "Display names are labels only; requests use the API model ID."
        ))
        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, 1)

        actions = QHBoxLayout()
        self.add_btn = QPushButton("Add Model")
        self.edit_btn = QPushButton("Edit")
        self.delete_btn = QPushButton("Delete")
        actions.addWidget(self.add_btn)
        actions.addWidget(self.edit_btn)
        actions.addWidget(self.delete_btn)
        actions.addStretch()
        layout.addLayout(actions)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.add_btn.clicked.connect(self._add_model)
        self.edit_btn.clicked.connect(self._edit_model)
        self.delete_btn.clicked.connect(self._delete_model)
        self._refresh()

    def model_records(self):
        return [dict(item) for item in self._models]

    def _refresh(self, selected_model_id=None):
        self.list_widget.clear()
        selected_row = -1
        for row, item in enumerate(self._models):
            self.list_widget.addItem(
                f"{item['display_name']}  ·  {item['model_id']}"
            )
            if item["model_id"] == selected_model_id:
                selected_row = row
        if self.list_widget.count():
            self.list_widget.setCurrentRow(selected_row if selected_row >= 0 else 0)
        self.edit_btn.setEnabled(bool(self._models))
        self.delete_btn.setEnabled(bool(self._models))

    def _ask_model(self, existing=None):
        name, ok = QInputDialog.getText(
            self, "Custom Model", "Display name (for example, OpenAI 120B):",
            text=(existing or {}).get("display_name", ""),
        )
        if not ok:
            return None
        model_id, ok = QInputDialog.getText(
            self, "Custom Model", "Exact API model ID (sent unchanged to the provider):",
            text=(existing or {}).get("model_id", ""),
        )
        if not ok:
            return None
        old_id = (existing or {}).get("model_id")
        try:
            return ai_assist.validate_custom_model(
                self.provider_id, name, model_id, self._models,
                exclude_model_id=old_id,
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Custom Model", str(exc))
            return None

    def _add_model(self):
        entry = self._ask_model()
        if entry is None:
            return
        self._models.append(entry)
        self._refresh(entry["model_id"])

    def _edit_model(self):
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._models):
            return
        previous = self._models[row]
        entry = self._ask_model(previous)
        if entry is None:
            return
        self._models[row] = entry
        self._refresh(entry["model_id"])

    def _delete_model(self):
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._models):
            return
        self._models.pop(row)
        self._refresh()



class SettingsDialog(QDialog):
    def __init__(self, parent=None, k8s_tab_titles=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setFixedSize(460, 540)
        apply_qss_to(self)
        settings = load_settings()
        self._k8s_titles = list(k8s_tab_titles or [])
        self._hidden = set(settings.get("k8s_hidden_tabs", []))
        self._lock = security.get_lock_settings()
        self._pending_pin = None
        self._webapp_otp_secret = None
        self._webapp_otp_timer = None
        self._webapp_enabled = bool(settings.get("webapp_enabled", False))
        self._webapp_original_enabled = self._webapp_enabled

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(10)

        title = QLabel("⚙  Settings")
        title.setFont(QFont("Segoe UI", 15, QFont.Bold))
        title.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        lay.addWidget(title)

        tabs = SwipeTabWidget()
        tabs.addTab(self._build_security_tab(), " 🔒  Security ")
        tabs.addTab(self._build_k8s_tab(), " ⎈  Kubernetes Tabs ")
        tabs.addTab(self._build_ai_tab(), " 🤖  AI ")
        tabs.addTab(self._build_webapp_tab(), " 🌐  Web App ")
        lay.addWidget(tabs, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Save")
        btns.button(QDialogButtonBox.Ok).setObjectName("primary")
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _build_security_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)
        v.setContentsMargins(4, 14, 4, 4)

        self.lock_chk = QCheckBox("Require a PIN to open this app")
        self.lock_chk.setChecked(self._lock["enabled"])
        self.lock_chk.toggled.connect(self._on_lock_toggled)
        v.addWidget(self.lock_chk)

        desc = QLabel("Locks the app on launch, and again after a period of inactivity.")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        v.addWidget(desc)
        v.addSpacing(4)

        self.pin_btn = QPushButton(
            "Change PIN…" if self._lock["pin_hash"] else "Set PIN…"
        )
        self.pin_btn.clicked.connect(self._on_set_pin)
        v.addWidget(self.pin_btn)

        self.pin_status = QLabel(
            "PIN is set." if self._lock["pin_hash"] else "No PIN set yet."
        )
        self.pin_status.setStyleSheet(
            f"color: {T['TEXT_DIM']}; font-size: 12px;"
        )
        v.addWidget(self.pin_status)
        v.addSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("Auto-lock after"))
        self.autolock_combo = QComboBox()
        for label, mins in _AUTOLOCK_OPTIONS:
            self.autolock_combo.addItem(label, mins)
        idx = self.autolock_combo.findData(self._lock["autolock_minutes"])
        self.autolock_combo.setCurrentIndex(idx if idx >= 0 else 2)
        row.addWidget(self.autolock_combo)
        row.addStretch()
        v.addLayout(row)
        v.addStretch()

        self._update_security_enabled_state()
        return w

    def _on_lock_toggled(self, checked):
        if checked and not self._lock["pin_hash"] and not self._pending_pin:
            self._on_set_pin()
            if not self._pending_pin:
                self.lock_chk.blockSignals(True)
                self.lock_chk.setChecked(False)
                self.lock_chk.blockSignals(False)
        self._update_security_enabled_state()

    def _update_security_enabled_state(self):
        self.autolock_combo.setEnabled(self.lock_chk.isChecked())

    def _on_set_pin(self):
        dlg = SetPinDialog(self)
        if dlg.exec_() == QDialog.Accepted:
            self._pending_pin = dlg.pin_value()
            self.pin_status.setText("PIN is set.")
            self.pin_btn.setText("Change PIN…")
            if not self.lock_chk.isChecked():
                self.lock_chk.blockSignals(True)
                self.lock_chk.setChecked(True)
                self.lock_chk.blockSignals(False)
                self._update_security_enabled_state()

    def _build_k8s_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(6)
        v.setContentsMargins(4, 14, 4, 4)

        desc = QLabel("Choose which Kubernetes sub-tabs are visible.")
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        v.addWidget(desc)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        iv.setSpacing(4)
        iv.setContentsMargins(2, 6, 2, 6)
        self._k8s_checks = {}

        for title in self._k8s_titles:
            chk = QCheckBox(title.strip())
            chk.setChecked(title not in self._hidden)
            iv.addWidget(chk)
            self._k8s_checks[title] = chk

        iv.addStretch()
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)
        v.addSpacing(4)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setStyleSheet(f"color: {T['BORDER']};")
        v.addWidget(line)
        v.addSpacing(4)

        protected_lbl = QLabel("Protected namespaces (Ops Mind)")
        protected_lbl.setStyleSheet(
            f"color: {T['TEXT_PRIMARY']}; font-weight: 600;"
        )
        v.addWidget(protected_lbl)

        protected_desc = QLabel(
            "Comma-separated name fragments. When the AI Kubernetes Operations "
            "panel targets a namespace matching one of these (e.g. \"mcp\" "
            "matches \"mcp-prod-eks\"), scale requests require confirmation "
            "just like delete already does. Restart always confirms."
        )
        protected_desc.setWordWrap(True)
        protected_desc.setStyleSheet(
            f"color: {T['TEXT_MUTED']}; font-size: 12px;"
        )
        v.addWidget(protected_desc)

        self.protected_ns_edit = QLineEdit()
        self.protected_ns_edit.setText(", ".join(k8s_ai_ops.get_protected_namespaces()))
        self.protected_ns_edit.setPlaceholderText("prod, production, mcp")
        v.addWidget(self.protected_ns_edit)
        return w

    def _build_ai_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)
        v.setContentsMargins(4, 14, 4, 4)

        desc = QLabel(
            "Used by the ✨ Explain button in pod log/exec views to get an AI "
            "diagnosis of crashes and errors. Pick a provider, choose (or type) "
            "a model, and paste the matching API key — each is stored "
            "per-provider and sent only to that provider's own API."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        v.addWidget(desc)

        ai_settings = ai_assist.get_ai_settings()
        self._ai_keys = dict(ai_settings["api_keys"])
        self._ai_models = dict(ai_settings["models"])
        self._ai_custom_models = {
            pid: [dict(item) for item in records]
            for pid, records in ai_settings.get("custom_models", {}).items()
        }
        self._current_provider_id = None

        v.addSpacing(4)
        v.addWidget(QLabel("Provider"))
        self.provider_combo = QComboBox()
        for pid, info in ai_assist.PROVIDERS.items():
            self.provider_combo.addItem(info["label"], pid)
        v.addWidget(self.provider_combo)

        v.addWidget(QLabel("Model"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        v.addWidget(self.model_combo)

        model_actions = QHBoxLayout()
        self.add_model_btn = QPushButton("Add Model")
        self.manage_models_btn = QPushButton("Manage Models…")
        self.test_model_btn = QPushButton("Test Model")
        model_actions.addWidget(self.add_model_btn)
        model_actions.addWidget(self.manage_models_btn)
        model_actions.addWidget(self.test_model_btn)
        v.addLayout(model_actions)

        v.addWidget(QLabel("API key"))
        row = QHBoxLayout()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        row.addWidget(self.api_key_edit, 1)

        self.show_key_btn = QPushButton("Show")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setFixedWidth(60)
        self.show_key_btn.toggled.connect(self._on_toggle_show_key)
        row.addWidget(self.show_key_btn)
        v.addLayout(row)

        profile_group = QLabel("Task-specific model routing")
        profile_group.setStyleSheet("font-weight: 600;")
        v.addWidget(profile_group)
        profile_help = QLabel(
            "Choose which configured provider handles quick command explanations, "
            "deep pod diagnoses, voice/conversation, and private local inference. "
            "Ollama uses only http://127.0.0.1:11434 and does not require an API key."
        )
        profile_help.setWordWrap(True)
        profile_help.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 12px;")
        v.addWidget(profile_help)

        self.task_profile_combos = {}
        self.task_profile_model_combos = {}
        for profile_id, label in (
            ("quick", "Quick explanations"),
            ("deep_diagnosis", "Deep diagnoses"),
            ("voice", "Voice / conversation"),
            ("local_private", "Local / Private (Ollama loopback)"),
        ):
            v.addWidget(QLabel(label))
            provider_combo = QComboBox()
            provider_choices = (
                [("ollama", ai_assist.PROVIDERS["ollama"])]
                if profile_id == "local_private"
                else list(ai_assist.PROVIDERS.items())
            )
            for provider_id, provider_info in provider_choices:
                provider_combo.addItem(provider_info["label"], provider_id)
            if profile_id == "local_private":
                provider_combo.setEnabled(False)
            profile_settings = ai_settings.get("task_profiles", {}).get(profile_id, {})
            selected = profile_settings.get(
                "provider", "ollama" if profile_id == "local_private" else ai_settings["provider"]
            )
            index = provider_combo.findData(selected)
            provider_combo.setCurrentIndex(index if index >= 0 else 0)
            self.task_profile_combos[profile_id] = provider_combo
            v.addWidget(provider_combo)

            model_combo = QComboBox()
            model_combo.setEditable(True)
            self.task_profile_model_combos[profile_id] = model_combo
            self._refresh_task_profile_models(
                profile_id, preferred_model=profile_settings.get("model_id")
            )
            provider_combo.currentIndexChanged.connect(
                lambda _index, pid=profile_id: self._on_task_profile_provider_changed(pid)
            )
            v.addWidget(model_combo)

        self.auto_escalation_chk = QCheckBox(
            "Automatically escalate failed/large command explanations to Deep Diagnosis"
        )
        self.auto_escalation_chk.setChecked(bool(ai_settings.get("auto_escalation", False)))
        v.addWidget(self.auto_escalation_chk)

        self.usage_telemetry_chk = QCheckBox(
            "Enable local aggregate AI usage/latency counters (no prompts, logs, or API keys stored)"
        )
        self.usage_telemetry_chk.setChecked(bool(ai_settings.get("usage_telemetry_enabled", False)))
        v.addWidget(self.usage_telemetry_chk)

        self.ai_intelligence_tools_btn = QPushButton("Open AI Intelligence Workspace…")
        self.ai_intelligence_tools_btn.setToolTip(
            "Open incident-adjacent analysis, cluster comparison, watch planning, "
            "resource recommendations, remediation validation, and service indexing tools."
        )
        self.ai_intelligence_tools_btn.clicked.connect(self._open_ai_intelligence_workspace)
        v.addWidget(self.ai_intelligence_tools_btn)

        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.add_model_btn.clicked.connect(self._add_custom_model)
        self.manage_models_btn.clicked.connect(self._manage_custom_models)
        self.test_model_btn.clicked.connect(self._test_selected_model)
        idx = self.provider_combo.findData(ai_settings["provider"])
        self.provider_combo.setCurrentIndex(idx if idx >= 0 else 0)
        if self._current_provider_id is None:
            self._on_provider_changed(self.provider_combo.currentIndex())
        v.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(w)
        return scroll

    def _refresh_task_profile_models(self, profile_id, preferred_model=None):
        provider_combo = self.task_profile_combos.get(profile_id)
        model_combo = self.task_profile_model_combos.get(profile_id)
        if provider_combo is None or model_combo is None:
            return
        provider_id = provider_combo.currentData()
        info = ai_assist.PROVIDERS.get(provider_id)
        if not info:
            return
        model_combo.blockSignals(True)
        model_combo.clear()
        for model_id in info["model_samples"]:
            model_combo.addItem(model_id, model_id)
        for item in self._ai_custom_models.get(provider_id, []):
            model_id = item.get("model_id")
            if model_id and model_combo.findData(model_id) < 0:
                model_combo.addItem(model_id, model_id)
        default_model = self._ai_models.get(provider_id) or info["default_model"]
        desired = preferred_model or default_model
        index = model_combo.findData(desired)
        if index >= 0:
            model_combo.setCurrentIndex(index)
        else:
            model_combo.setEditText(desired)
        model_combo.blockSignals(False)

    def _on_task_profile_provider_changed(self, profile_id):
        provider_id = self.task_profile_combos[profile_id].currentData()
        self._refresh_task_profile_models(
            profile_id, preferred_model=self._ai_models.get(provider_id)
        )

    def _selected_task_profile_model(self, profile_id):
        combo = self.task_profile_model_combos[profile_id]
        index = combo.currentIndex()
        if index >= 0 and combo.currentText() == combo.itemText(index):
            value = combo.itemData(index)
            if value is not None:
                return str(value)
        return combo.currentText().strip()

    def _open_ai_intelligence_workspace(self):
        from ai_intelligence_ui import open_ai_intelligence_workspace
        open_ai_intelligence_workspace(self)

    def _selected_model_id(self):
        index = self.model_combo.currentIndex()
        if (
            index >= 0
            and self.model_combo.currentText() == self.model_combo.itemText(index)
        ):
            value = self.model_combo.itemData(index)
            if value is not None:
                return str(value)
        return self.model_combo.currentText().strip()

    def _refresh_model_combo(self, preferred_model=None):
        pid = self.provider_combo.currentData()
        info = ai_assist.PROVIDERS[pid]
        current_model = (
            preferred_model
            if preferred_model is not None
            else self._ai_models.get(pid) or info["default_model"]
        )
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for model_id in info["model_samples"]:
            self.model_combo.addItem(f"Built-in · {model_id}", model_id)
        for item in self._ai_custom_models.get(pid, []):
            self.model_combo.addItem(
                f"Custom · {item['display_name']} — {item['model_id']}",
                item["model_id"],
            )
        model_idx = self.model_combo.findData(current_model)
        if model_idx >= 0:
            self.model_combo.setCurrentIndex(model_idx)
        else:
            # Preserve legacy free-text model IDs that are not in suggestions.
            self.model_combo.setEditText(current_model)
        self.model_combo.blockSignals(False)

    def _on_provider_changed(self, _index):
        if self._current_provider_id is not None:
            old_provider = self._current_provider_id
            self._ai_keys[old_provider] = self.api_key_edit.text()
            self._ai_models[old_provider] = self._selected_model_id()

        pid = self.provider_combo.currentData()
        if pid not in ai_assist.PROVIDERS:
            return
        info = ai_assist.PROVIDERS[pid]
        self._current_provider_id = pid
        self._refresh_model_combo()
        self.api_key_edit.setText(self._ai_keys.get(pid, ""))
        self.api_key_edit.setPlaceholderText(info["key_placeholder"])

    def _add_custom_model(self):
        pid = self.provider_combo.currentData()
        name, ok = QInputDialog.getText(
            self, "Add Custom AI Model", "Display name (label only):"
        )
        if not ok:
            return
        model_id, ok = QInputDialog.getText(
            self, "Add Custom AI Model", "Exact API model ID:"
        )
        if not ok:
            return
        try:
            entry = ai_assist.validate_custom_model(
                pid, name, model_id, self._ai_custom_models.get(pid, [])
            )
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid Custom Model", str(exc))
            return
        self._ai_custom_models.setdefault(pid, []).append(entry)
        self._refresh_model_combo(entry["model_id"])

    def _manage_custom_models(self):
        pid = self.provider_combo.currentData()
        dialog = CustomModelManagerDialog(
            pid, self._ai_custom_models.get(pid, []), self
        )
        if dialog.exec_() != QDialog.Accepted:
            return
        before = {item["model_id"] for item in self._ai_custom_models.get(pid, [])}
        records = dialog.model_records()
        after = {item["model_id"] for item in records}
        selected = self._selected_model_id()
        self._ai_custom_models[pid] = records
        if selected in before and selected not in after:
            selected = ai_assist.PROVIDERS[pid]["default_model"]
            self._ai_models[pid] = selected
        self._refresh_model_combo(selected)

    def _test_selected_model(self):
        pid = self.provider_combo.currentData()
        api_key = self.api_key_edit.text().strip()
        model_id = self._selected_model_id()
        if not api_key and pid != "ollama":
            QMessageBox.information(
                self, "API key required",
                f"Add a {ai_assist.PROVIDERS[pid]['label']} API key before testing the model.",
            )
            return
        if not ai_assist._is_valid_model_id(model_id):
            QMessageBox.warning(
                self, "Invalid model ID",
                "Enter a valid exact API model ID or select a listed model.",
            )
            return
        self.test_model_btn.setEnabled(False)
        self.test_model_btn.setText("Testing…")
        worker = ai_assist.AIModelTestWorker(pid, api_key, model_id, self)
        self._model_test_worker = worker
        worker.succeeded.connect(self._on_model_test_success)
        worker.failed.connect(self._on_model_test_failure)
        worker.finished.connect(self._on_model_test_finished)
        worker.start()

    def _on_model_test_success(self, message):
        QMessageBox.information(self, "Model Test Succeeded", message)

    def _on_model_test_failure(self, message):
        QMessageBox.warning(self, "Model Test Failed", message)

    def _on_model_test_finished(self):
        self.test_model_btn.setEnabled(True)
        self.test_model_btn.setText("Test Model")

    def _on_toggle_show_key(self, checked):
        self.api_key_edit.setEchoMode(
            QLineEdit.Normal if checked else QLineEdit.Password
        )
        self.show_key_btn.setText("Hide" if checked else "Show")

    def _build_webapp_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(8)
        v.setContentsMargins(4, 14, 4, 4)
        self.webapp_enable_btn = QPushButton()
        self.webapp_enable_btn.setCheckable(True)
        self.webapp_enable_btn.setChecked(self._webapp_enabled)
        self.webapp_enable_btn.setMinimumHeight(38)
        self.webapp_enable_btn.clicked.connect(self._on_webapp_toggle)
        v.addWidget(self.webapp_enable_btn)

        self.webapp_status_lbl = QLabel()
        self.webapp_status_lbl.setWordWrap(True)
        self.webapp_status_lbl.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        v.addWidget(self.webapp_status_lbl)
        self._update_webapp_toggle_button(self._webapp_enabled)
        desc = QLabel("The dashboard uses a six-digit TOTP code that changes every 30 seconds. The shared seed stays in the OS credential vault.")
        desc.setWordWrap(True)
        v.addWidget(desc)
        v.addWidget(QLabel("Current Web App OTP"))
        self.webapp_otp_code = QLabel("------")
        self.webapp_otp_code.setAlignment(Qt.AlignCenter)
        self.webapp_otp_code.setMinimumHeight(46)
        self.webapp_otp_code.setStyleSheet(f"color: {T['ACCENT']}; font-size: 27px; font-weight: 600; letter-spacing: 5px; font-family: monospace;")
        v.addWidget(self.webapp_otp_code)
        otp_row = QHBoxLayout()
        self.webapp_otp_countdown = QLabel("Remaining: --s")
        self.webapp_otp_countdown.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        otp_row.addWidget(self.webapp_otp_countdown, 1)
        self.webapp_copy_otp_btn = QPushButton("Copy code")
        self.webapp_copy_otp_btn.setEnabled(False)
        self.webapp_copy_otp_btn.clicked.connect(self._copy_webapp_otp)
        otp_row.addWidget(self.webapp_copy_otp_btn)
        v.addLayout(otp_row)
        warning = QLabel("Remote devices require HTTPS. Set WEBAPP_TLS_CERTFILE and WEBAPP_TLS_KEYFILE before exposing the Web App.")
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 11px;")
        v.addWidget(warning)
        try:
            from webapp import get_server_url
            login_url = get_server_url()
        except (ImportError, AttributeError):
            login_url = "http://127.0.0.1:8000"
        self.webapp_link = QLabel(f'<a href="{login_url}">🔗 Open Web App Login</a>')
        self.webapp_link.setOpenExternalLinks(True)
        self.webapp_link.setStyleSheet(f"color: {T['ACCENT']};")
        self.webapp_link.setToolTip(login_url)
        v.addWidget(self.webapp_link)
        self.webapp_url_label = QLabel(login_url)
        self.webapp_url_label.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        v.addWidget(self.webapp_url_label)
        v.addStretch()
        try:
            from webapp.otp_auth import get_or_create_secret
            self._webapp_otp_secret = get_or_create_secret()
            self._refresh_webapp_otp()
            self._webapp_otp_timer = QTimer(self)
            self._webapp_otp_timer.setInterval(250)
            self._webapp_otp_timer.timeout.connect(self._refresh_webapp_otp)
            self._webapp_otp_timer.start()
        except Exception:
            self.webapp_otp_code.setText("Unavailable")
            self.webapp_otp_countdown.setText("Secure OS credential vault unavailable")
            self.webapp_copy_otp_btn.setEnabled(False)
            self.webapp_status_lbl.setText("OTP is unavailable. Enable a supported OS credential vault before using Web App login.")
        return w

    def _refresh_webapp_otp(self):
        if not self._webapp_otp_secret:
            return
        try:
            import time
            from webapp.otp_auth import TOTP_PERIOD_SECONDS, totp
            now = time.time()
            self._webapp_current_otp = totp(self._webapp_otp_secret, now)
            self.webapp_otp_code.setText(f"{self._webapp_current_otp[:3]} {self._webapp_current_otp[3:]}")
            self.webapp_otp_countdown.setText(f"Refreshes in {TOTP_PERIOD_SECONDS - (int(now) % TOTP_PERIOD_SECONDS)}s")
            self.webapp_copy_otp_btn.setEnabled(True)
        except Exception:
            self.webapp_otp_code.setText("Unavailable")
            self.webapp_otp_countdown.setText("Unable to generate current code")
            self.webapp_copy_otp_btn.setEnabled(False)

    def _copy_webapp_otp(self):
        code = getattr(self, "_webapp_current_otp", "")
        if code:
            QApplication.clipboard().setText(code)
            self.webapp_status_lbl.setText("Current OTP copied. It expires at the end of this time window.")

    def _on_webapp_toggle(self, checked):
        """Apply the Web App state immediately when the toggle is clicked."""
        self._update_webapp_toggle_button(checked)
        try:
            from webapp import app as webapp_app
            main_window = self.parent()
            if checked:
                webapp_app.start_server(lambda: getattr(main_window, "ssh", None), app_provider=lambda: main_window)
                self.webapp_status_lbl.setText("Web App enabled and starting now.")
            else:
                webapp_app.stop_server()
                self.webapp_status_lbl.setText("Web App disabled and stopped now.")
        except Exception as exc:
            self.webapp_status_lbl.setText(
                f"Web App state changed, but server action failed: {exc}"
            )

    def _update_webapp_toggle_button(self, checked):
        self.webapp_enable_btn.blockSignals(True)
        self.webapp_enable_btn.setChecked(bool(checked))
        self.webapp_enable_btn.setText(
            "●  Web App Enabled — Click to Disable"
            if checked else
            "○  Web App Disabled — Click to Enable"
        )
        self.webapp_enable_btn.blockSignals(False)
        self.webapp_status_lbl.setText(
            "Web App is currently enabled."
            if checked else
            "Web App is currently disabled."
        )

    def _on_save(self):
        if self.lock_chk.isChecked():
            if self._pending_pin:
                security.set_pin(
                    self._pending_pin,
                    self.autolock_combo.currentData(),
                )
            elif self._lock["pin_hash"]:
                security.save_lock_settings(
                    enabled=True,
                    autolock_minutes=self.autolock_combo.currentData(),
                )
            else:
                security.disable_lock()
        else:
            security.save_lock_settings(enabled=False)

        save_settings(k8s_hidden_tabs=self.hidden_k8s_tabs())
        k8s_ai_ops.save_protected_namespaces(
            [p.strip() for p in self.protected_ns_edit.text().split(",")]
        )
        self._ai_keys[self._current_provider_id] = self.api_key_edit.text()
        self._ai_models[self._current_provider_id] = self._selected_model_id()
        ai_assist.save_ai_settings(
            self.provider_combo.currentData(),
            self._ai_keys,
            self._ai_models,
            custom_models=self._ai_custom_models,
            task_profiles={
                profile_id: {
                    "provider": combo.currentData(),
                    "model_id": self._selected_task_profile_model(profile_id),
                }
                for profile_id, combo in self.task_profile_combos.items()
            },
            auto_escalation=self.auto_escalation_chk.isChecked(),
            usage_telemetry_enabled=self.usage_telemetry_chk.isChecked(),
        )
        save_settings(
            webapp_enabled=self.webapp_enable_btn.isChecked(),
            webapp_username="",
            webapp_password="",
            webapp_password_salt="",
            webapp_password_hash="",
        )

        self._webapp_original_enabled = self.webapp_enable_btn.isChecked()
        if self._webapp_otp_timer:
            self._webapp_otp_timer.stop()
        self.accept()

    def reject(self):
        # Cancel restores the server state that was active when Settings opened.
        try:
            from webapp import app as webapp_app
            main_window = self.parent()
            if self._webapp_original_enabled:
                webapp_app.start_server(lambda: getattr(main_window, "ssh", None), app_provider=lambda: main_window)
            else:
                webapp_app.stop_server()
        except Exception:
            pass
        if self._webapp_otp_timer:
            self._webapp_otp_timer.stop()
        super().reject()

    def hidden_k8s_tabs(self):
        return [t for t, chk in self._k8s_checks.items() if not chk.isChecked()]
