"""settings_dialog.py — App-wide Settings dialog."""

from pathlib import Path
from PyQt5.QtCore import QEvent, QTimer, Qt, QUrl
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QCheckBox,
    QComboBox, QTabBar, QTabWidget, QWidget, QScrollArea, QDialogButtonBox,
    QFrame, QLineEdit, QApplication, QListWidget, QInputDialog, QMessageBox,
    QSpinBox, QGroupBox, QGridLayout, QFormLayout,
)
from PyQt5.QtGui import QFont, QDesktopServices
from db import APP_DIR, connect as db_connect
from app_preferences import PERFORMANCE_PROFILES, normalize_performance_settings, normalize_connection_preferences
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
        self.setObjectName("settingsDialog")
        # Keep Settings feeling like a dialog, not a maximized application
        # window. The upper bounds adapt to smaller displays.
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen is not None else None
        if available is not None:
            max_width = min(1040, max(480, available.width() - 48))
            max_height = min(860, max(480, available.height() - 48))
            width = min(960, max(680, int(available.width() * 0.78)), max_width)
            height = min(800, max(580, int(available.height() * 0.82)), max_height)
        else:
            max_width, max_height = 1040, 860
            width, height = 960, 800
        self.setMaximumSize(max_width, max_height)
        self.setMinimumSize(min(720, width), min(560, height))
        self.resize(width, height)
        self.setSizeGripEnabled(True)
        apply_qss_to(self)
        # Settings uses quieter, readable headings rather than the dense,
        # letter-spaced section labels used by the main application panels.
        self.setStyleSheet(self.styleSheet() + f"""
            QDialog#settingsDialog QGroupBox {{
                color: {T['TEXT_PRIMARY']};
                border: 1px solid {T['BORDER']};
                border-radius: 10px;
                margin-top: 14px;
                padding: 14px 12px 12px 12px;
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0;
            }}
            QDialog#settingsDialog QGroupBox::title {{
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
                color: {T['TEXT_PRIMARY']};
                font-size: 13px;
                font-weight: 600;
                letter-spacing: 0;
            }}
            QDialog#settingsDialog QTabBar::tab {{
                padding: 10px 12px;
                font-size: 12px;
            }}
        """)
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
        tabs.addTab(self._build_general_tab(), "⚙ General")
        tabs.addTab(self._build_connections_tab(), "⌁ Connections")
        tabs.addTab(self._build_security_tab(), "🔒 Security")
        tabs.addTab(self._build_k8s_tab(), "⎈ Kubernetes")
        tabs.addTab(self._build_ai_tab(), "🤖 AI")
        tabs.addTab(self._build_webapp_tab(), "🌐 Web App")
        tabs.tabBar().setExpanding(False)
        tabs.setUsesScrollButtons(True)
        lay.addWidget(tabs, 1)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.button(QDialogButtonBox.Ok).setText("Save")
        btns.button(QDialogButtonBox.Ok).setObjectName("primary")
        btns.accepted.connect(self._on_save)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

    def _build_general_tab(self):
        w = QWidget()
        root = QGridLayout(w)
        root.setContentsMargins(12, 12, 12, 12)
        root.setHorizontalSpacing(14)
        root.setVerticalSpacing(12)

        intro = QLabel(
            "Control refresh behaviour and local maintenance. Changes are saved on this device."
        )
        intro.setWordWrap(True)
        intro.setObjectName("secondary_text")
        root.addWidget(intro, 0, 0, 1, 2)

        perf = normalize_performance_settings(load_settings())
        self._performance_values = {}
        self._applying_performance_profile = False

        performance_group = QGroupBox("Performance and refresh")
        performance_layout = QVBoxLayout(performance_group)
        performance_layout.setContentsMargins(12, 18, 12, 12)
        performance_layout.setSpacing(12)

        profile_row = QHBoxLayout()
        profile_label = QLabel("Refresh profile")
        profile_label.setMinimumWidth(110)
        profile_row.addWidget(profile_label)
        self.performance_profile_combo = QComboBox()
        for label, key in (
            ("Low resource usage", "low_resource"),
            ("Balanced", "balanced"),
            ("Responsive", "responsive"),
            ("Custom", "custom"),
        ):
            self.performance_profile_combo.addItem(label, key)
        self.performance_profile_combo.setCurrentIndex(
            max(0, self.performance_profile_combo.findData(perf["profile"]))
        )
        self.performance_profile_combo.setMaximumWidth(260)
        profile_row.addWidget(self.performance_profile_combo)
        profile_row.addStretch(1)
        performance_layout.addLayout(profile_row)

        interval_grid = QGridLayout()
        interval_grid.setHorizontalSpacing(10)
        interval_grid.setVerticalSpacing(10)
        interval_grid.setColumnStretch(0, 1)
        interval_grid.setColumnStretch(1, 1)
        field_spec = (
            ("dashboard_refresh_seconds", "Dashboard while open", 1, 60),
            ("background_refresh_seconds", "Dashboard in background", 10, 600),
            ("kubernetes_refresh_seconds", "Kubernetes auto-refresh", 5, 600),
            ("docker_refresh_seconds", "Docker auto-refresh", 3, 300),
            ("tunnel_refresh_seconds", "Tunnel status polling", 5, 300),
        )
        for index, (key, label, low, high) in enumerate(field_spec):
            tile = QFrame()
            tile.setObjectName("refreshTile")
            tile.setStyleSheet(
                f"QFrame#refreshTile {{ background: {T['BG_PANEL']}; "
                f"border: 1px solid {T['BORDER']}; border-radius: 8px; }}"
                f"QFrame#refreshTile QLabel {{ background: transparent; border: none; "
                f"color: {T['TEXT_DIM']}; font-size: 11px; }}"
            )
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(10, 8, 10, 8)
            tile_layout.setSpacing(6)
            label_widget = QLabel(label)
            label_widget.setWordWrap(True)
            tile_layout.addWidget(label_widget)
            spin = QSpinBox()
            spin.setRange(low, high)
            spin.setSuffix(" s")
            spin.setSingleStep(1 if high <= 60 else 5)
            spin.setKeyboardTracking(False)
            spin.setValue(perf[key])
            spin.setMinimumWidth(100)
            spin.setMaximumWidth(135)
            tile_layout.addWidget(spin, 0, Qt.AlignLeft)
            self._performance_values[key] = spin
            spin.valueChanged.connect(self._mark_custom_performance)
            interval_grid.addWidget(tile, index // 2, index % 2)
        performance_layout.addLayout(interval_grid)

        self.pause_dashboard_chk = QCheckBox(
            "Pause dashboard polling when the Dashboard tab is inactive"
        )
        self.pause_dashboard_chk.setChecked(perf["pause_dashboard_when_inactive"])
        self.pause_dashboard_chk.setToolTip(
            "Stops automatic dashboard polling while another main page is selected."
        )
        performance_layout.addWidget(self.pause_dashboard_chk)
        hint = QLabel("Manual Refresh actions remain available in every profile.")
        hint.setWordWrap(True)
        hint.setObjectName("secondary_text")
        performance_layout.addWidget(hint)
        root.addWidget(performance_group, 1, 0, 2, 1, Qt.AlignTop)

        appearance = QGroupBox("Appearance and accessibility")
        appearance_layout = QVBoxLayout(appearance)
        appearance_layout.setContentsMargins(12, 18, 12, 12)
        self.reduce_motion_chk = QCheckBox(
            "Reduce motion (disable animated page transitions)"
        )
        self.reduce_motion_chk.setChecked(bool(load_settings().get("reduce_motion", False)))
        appearance_layout.addWidget(self.reduce_motion_chk)
        appearance_hint = QLabel(
            "Useful when you prefer less movement while switching between pages."
        )
        appearance_hint.setWordWrap(True)
        appearance_hint.setObjectName("secondary_text")
        appearance_layout.addWidget(appearance_hint)
        root.addWidget(appearance, 1, 1, Qt.AlignTop)

        maintenance = QGroupBox("Maintenance")
        maintenance_layout = QVBoxLayout(maintenance)
        maintenance_layout.setContentsMargins(12, 18, 12, 12)
        maintenance_layout.setSpacing(8)
        open_folder = QPushButton("Open KubeDock data folder")
        open_folder.clicked.connect(self._open_app_data_folder)
        maintenance_layout.addWidget(open_folder)
        clear_history = QPushButton("Clear dashboard history…")
        clear_history.clicked.connect(self._clear_dashboard_history)
        maintenance_layout.addWidget(clear_history)
        maintenance_hint = QLabel(
            "History cleanup does not remove saved connections, credentials, or settings."
        )
        maintenance_hint.setWordWrap(True)
        maintenance_hint.setObjectName("secondary_text")
        maintenance_layout.addWidget(maintenance_hint)
        root.addWidget(maintenance, 2, 1, Qt.AlignTop)

        root.setColumnStretch(0, 3)
        root.setColumnStretch(1, 2)
        root.setRowStretch(3, 1)
        self.performance_profile_combo.currentIndexChanged.connect(
            self._on_performance_profile_changed
        )
        self._set_performance_controls_enabled(perf["profile"] == "custom")
        return w

    def _on_performance_profile_changed(self, _index):
        profile = self.performance_profile_combo.currentData()
        if profile in PERFORMANCE_PROFILES:
            self._applying_performance_profile = True
            try:
                for key, value in PERFORMANCE_PROFILES[profile].items():
                    self._performance_values[key].setValue(value)
            finally:
                self._applying_performance_profile = False
        self._set_performance_controls_enabled(profile == "custom")

    def _mark_custom_performance(self, _value=None):
        if self._applying_performance_profile:
            return
        if self.performance_profile_combo.currentData() != "custom":
            self.performance_profile_combo.blockSignals(True)
            self.performance_profile_combo.setCurrentIndex(self.performance_profile_combo.findData("custom"))
            self.performance_profile_combo.blockSignals(False)
            self._set_performance_controls_enabled(True)

    def _set_performance_controls_enabled(self, enabled):
        for control in getattr(self, "_performance_values", {}).values():
            control.setEnabled(bool(enabled))

    def _open_app_data_folder(self):
        try:
            Path(APP_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(APP_DIR)))
        except Exception as exc:
            QMessageBox.warning(self, "Unable to open folder", str(exc))

    def _clear_dashboard_history(self):
        answer = QMessageBox.question(
            self, "Clear dashboard history",
            "Delete saved dashboard history from this device? This does not remove "
            "connection profiles, settings, or credentials.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            with db_connect() as database:
                database.execute("DELETE FROM dashboard_history")
            QMessageBox.information(self, "Dashboard history", "Dashboard history cleared.")
        except Exception as exc:
            QMessageBox.warning(self, "Unable to clear history", str(exc))

    def _build_connections_tab(self):
        w = QWidget()
        root = QGridLayout(w)
        root.setContentsMargins(12, 12, 12, 12)
        root.setHorizontalSpacing(14)
        root.setVerticalSpacing(12)
        intro = QLabel(
            "Choose default SSH behaviour and whether to remember Kubernetes selections for each connection."
        )
        intro.setWordWrap(True)
        intro.setObjectName("secondary_text")
        root.addWidget(intro, 0, 0, 1, 2)

        connection = normalize_connection_preferences(load_settings())
        ssh_group = QGroupBox("SSH defaults")
        ssh_form = QFormLayout(ssh_group)
        ssh_form.setContentsMargins(12, 18, 12, 12)
        ssh_form.setHorizontalSpacing(16)
        ssh_form.setVerticalSpacing(12)
        ssh_form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)

        self.ssh_timeout_spin = QSpinBox()
        self.ssh_timeout_spin.setRange(3, 60)
        self.ssh_timeout_spin.setSuffix(" s")
        self.ssh_timeout_spin.setValue(connection["ssh_connect_timeout_seconds"])
        self.ssh_timeout_spin.setMaximumWidth(145)
        ssh_form.addRow("Connect timeout", self.ssh_timeout_spin)

        self.ssh_keepalive_spin = QSpinBox()
        self.ssh_keepalive_spin.setRange(0, 120)
        self.ssh_keepalive_spin.setSuffix(" s (0 = disabled)")
        self.ssh_keepalive_spin.setValue(connection["ssh_keepalive_seconds"])
        self.ssh_keepalive_spin.setMaximumWidth(175)
        ssh_form.addRow("SSH keepalive interval", self.ssh_keepalive_spin)
        root.addWidget(ssh_group, 1, 0, Qt.AlignTop)

        remember_group = QGroupBox("Kubernetes selection")
        remember_layout = QVBoxLayout(remember_group)
        remember_layout.setContentsMargins(12, 18, 12, 12)
        remember_layout.setSpacing(8)
        self.remember_k8s_chk = QCheckBox(
            "Remember context and namespace for each connection"
        )
        self.remember_k8s_chk.setChecked(connection["remember_k8s_context"])
        remember_layout.addWidget(self.remember_k8s_chk)
        help_text = QLabel(
            "Selections are keyed by a one-way hash of host, port, and username. "
            "Private-key paths and contents are not copied into these preferences."
        )
        help_text.setWordWrap(True)
        help_text.setObjectName("secondary_text")
        remember_layout.addWidget(help_text)
        root.addWidget(remember_group, 1, 1, Qt.AlignTop)
        root.setColumnStretch(0, 1)
        root.setColumnStretch(1, 1)
        root.setRowStretch(2, 1)
        return w

    def _build_security_tab(self):
        w = QWidget()
        root = QGridLayout(w)
        root.setContentsMargins(12, 12, 12, 12)
        root.setHorizontalSpacing(14)
        root.setVerticalSpacing(12)

        lock_group = QGroupBox("App lock")
        lock_layout = QVBoxLayout(lock_group)
        lock_layout.setContentsMargins(12, 18, 12, 12)
        lock_layout.setSpacing(10)
        self.lock_chk = QCheckBox("Require a PIN to open this app")
        self.lock_chk.setChecked(self._lock["enabled"])
        self.lock_chk.toggled.connect(self._on_lock_toggled)
        lock_layout.addWidget(self.lock_chk)

        desc = QLabel(
            "KubeDock asks for the PIN at launch and again after the selected period of inactivity."
        )
        desc.setWordWrap(True)
        desc.setObjectName("secondary_text")
        lock_layout.addWidget(desc)

        self.pin_btn = QPushButton(
            "Change PIN…" if self._lock["pin_hash"] else "Set PIN…"
        )
        self.pin_btn.setMaximumWidth(220)
        self.pin_btn.clicked.connect(self._on_set_pin)
        lock_layout.addWidget(self.pin_btn, 0, Qt.AlignLeft)

        self.pin_status = QLabel(
            "PIN is set." if self._lock["pin_hash"] else "No PIN set yet."
        )
        self.pin_status.setObjectName("secondary_text")
        lock_layout.addWidget(self.pin_status)

        auto_row = QHBoxLayout()
        auto_row.addWidget(QLabel("Auto-lock after"))
        self.autolock_combo = QComboBox()
        for label, mins in _AUTOLOCK_OPTIONS:
            self.autolock_combo.addItem(label, mins)
        idx = self.autolock_combo.findData(self._lock["autolock_minutes"])
        self.autolock_combo.setCurrentIndex(idx if idx >= 0 else 2)
        self.autolock_combo.setMaximumWidth(180)
        auto_row.addWidget(self.autolock_combo)
        auto_row.addStretch(1)
        lock_layout.addLayout(auto_row)
        root.addWidget(lock_group, 0, 0, Qt.AlignTop)

        info_group = QGroupBox("Security notes")
        info_layout = QVBoxLayout(info_group)
        info_layout.setContentsMargins(12, 18, 12, 12)
        info_layout.setSpacing(8)
        info = QLabel(
            "Choose a PIN you do not reuse elsewhere. Keep the auto-lock delay short "
            "on shared machines. Changing the PIN does not change saved connection profiles."
        )
        info.setWordWrap(True)
        info.setObjectName("secondary_text")
        info_layout.addWidget(info)
        info_layout.addStretch(1)
        root.addWidget(info_group, 0, 1, Qt.AlignTop)
        root.setColumnStretch(0, 3)
        root.setColumnStretch(1, 2)
        root.setRowStretch(1, 1)
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
        v.setSpacing(10)
        v.setContentsMargins(12, 12, 12, 12)
        desc = QLabel(
            "Choose which Kubernetes sections are shown in the main view. Grouping here only changes Settings."
        )
        desc.setWordWrap(True)
        desc.setObjectName("secondary_text")
        v.addWidget(desc)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        inner = QWidget()
        inner.setMaximumWidth(900)
        grid = QGridLayout(inner)
        grid.setContentsMargins(2, 6, 2, 6)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        self._k8s_checks = {}
        grouped = {
            "Workloads": [],
            "Networking": [],
            "Storage and configuration": [],
            "Observability": [],
            "Tools": [],
        }
        for title in self._k8s_titles:
            lowered = title.strip().lower()
            if any(token in lowered for token in ("pod", "deployment", "stateful", "daemon", "hpa", "job")):
                group_name = "Workloads"
            elif any(token in lowered for token in ("service", "ingress", "tunnel")):
                group_name = "Networking"
            elif any(token in lowered for token in ("storage", "config", "secret", "persistent")):
                group_name = "Storage and configuration"
            elif any(token in lowered for token in ("event", "metric", "monitor", "history")):
                group_name = "Observability"
            else:
                group_name = "Tools"
            grouped[group_name].append(title)
        positions = {
            "Workloads": (0, 0),
            "Networking": (0, 1),
            "Storage and configuration": (1, 0),
            "Observability": (1, 1),
            "Tools": (2, 0),
        }
        for group_name, titles in grouped.items():
            if not titles:
                continue
            box = QGroupBox(group_name)
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(12, 18, 12, 10)
            box_layout.setSpacing(6)
            for title in titles:
                chk = QCheckBox(title.strip())
                chk.setChecked(title not in self._hidden)
                box_layout.addWidget(chk)
                self._k8s_checks[title] = chk
            row, col = positions[group_name]
            grid.addWidget(box, row, col, Qt.AlignTop)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        grid.setRowStretch(3, 1)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)

        protected_group = QGroupBox("Ops Mind protection")
        protected_layout = QVBoxLayout(protected_group)
        protected_layout.setContentsMargins(12, 18, 12, 12)
        protected_layout.setSpacing(8)
        protected_desc = QLabel(
            "Namespace name fragments that require extra confirmation for scale actions. "
            "For example, 'mcp' also matches 'mcp-prod-eks'. Restart actions always confirm."
        )
        protected_desc.setWordWrap(True)
        protected_desc.setObjectName("secondary_text")
        protected_layout.addWidget(protected_desc)
        self.protected_ns_edit = QLineEdit()
        self.protected_ns_edit.setText(", ".join(k8s_ai_ops.get_protected_namespaces()))
        self.protected_ns_edit.setPlaceholderText("prod, production, mcp")
        protected_layout.addWidget(self.protected_ns_edit)
        v.addWidget(protected_group)
        return w

    def _build_ai_tab(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(12)
        v.setContentsMargins(12, 12, 12, 12)

        desc = QLabel(
            "Configure the provider and model used for AI diagnosis, then route individual "
            "tasks to the models that suit them. API keys remain stored per provider."
        )
        desc.setWordWrap(True)
        desc.setObjectName("secondary_text")
        v.addWidget(desc)

        ai_settings = ai_assist.get_ai_settings()
        self._ai_keys = dict(ai_settings["api_keys"])
        self._ai_models = dict(ai_settings["models"])
        self._ai_custom_models = {
            pid: [dict(item) for item in records]
            for pid, records in ai_settings.get("custom_models", {}).items()
        }
        self._current_provider_id = None

        provider_group = QGroupBox("Provider configuration")
        provider_grid = QGridLayout(provider_group)
        provider_grid.setContentsMargins(12, 18, 12, 12)
        provider_grid.setHorizontalSpacing(12)
        provider_grid.setVerticalSpacing(10)
        provider_grid.addWidget(QLabel("Provider"), 0, 0)
        self.provider_combo = QComboBox()
        for pid, info in ai_assist.PROVIDERS.items():
            self.provider_combo.addItem(info["label"], pid)
        self.provider_combo.setMaximumWidth(360)
        provider_grid.addWidget(self.provider_combo, 0, 1)

        provider_grid.addWidget(QLabel("Model"), 1, 0)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        provider_grid.addWidget(self.model_combo, 1, 1)

        model_actions = QHBoxLayout()
        self.add_model_btn = QPushButton("Add Model")
        self.manage_models_btn = QPushButton("Manage Models…")
        self.test_model_btn = QPushButton("Test Model")
        model_actions.addWidget(self.add_model_btn)
        model_actions.addWidget(self.manage_models_btn)
        model_actions.addWidget(self.test_model_btn)
        model_actions.addStretch(1)
        provider_grid.addLayout(model_actions, 2, 1)

        provider_grid.addWidget(QLabel("API key"), 3, 0)
        key_row = QHBoxLayout()
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("Enter the API key for this provider")
        key_row.addWidget(self.api_key_edit, 1)
        self.show_key_btn = QPushButton("Show")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setFixedWidth(64)
        self.show_key_btn.toggled.connect(self._on_toggle_show_key)
        key_row.addWidget(self.show_key_btn)
        provider_grid.addLayout(key_row, 3, 1)
        provider_grid.setColumnMinimumWidth(0, 90)
        provider_grid.setColumnStretch(1, 1)
        v.addWidget(provider_group)

        routing_group = QGroupBox("Task-specific model routing")
        routing_layout = QGridLayout(routing_group)
        routing_layout.setContentsMargins(12, 18, 12, 12)
        routing_layout.setHorizontalSpacing(12)
        routing_layout.setVerticalSpacing(10)
        profile_help = QLabel(
            "Assign separate models for quick explanations, deeper diagnoses, voice, and "
            "private local inference. Ollama uses only http://127.0.0.1:11434 and needs no API key."
        )
        profile_help.setWordWrap(True)
        profile_help.setObjectName("secondary_text")
        routing_layout.addWidget(profile_help, 0, 0, 1, 2)

        response_row = QHBoxLayout()
        response_row.addWidget(QLabel("Default response detail"))
        self.ai_response_detail_combo = QComboBox()
        self.ai_response_detail_combo.addItem("Concise", "concise")
        self.ai_response_detail_combo.addItem("Balanced", "balanced")
        self.ai_response_detail_combo.addItem("Detailed", "detailed")
        current_detail = str(load_settings().get("ai_response_detail", "balanced") or "balanced")
        detail_index = self.ai_response_detail_combo.findData(current_detail)
        self.ai_response_detail_combo.setCurrentIndex(detail_index if detail_index >= 0 else 1)
        self.ai_response_detail_combo.setMaximumWidth(220)
        response_row.addWidget(self.ai_response_detail_combo)
        response_row.addStretch(1)
        routing_layout.addLayout(response_row, 1, 0, 1, 2)

        self.task_profile_combos = {}
        self.task_profile_model_combos = {}
        profiles = (
            ("quick", "Quick explanations"),
            ("deep_diagnosis", "Deep diagnoses"),
            ("voice", "Voice / conversation"),
            ("local_private", "Local / Private (Ollama loopback)"),
        )
        for index, (profile_id, label) in enumerate(profiles):
            profile_card = QFrame()
            profile_card.setObjectName("taskProfileCard")
            profile_card.setStyleSheet(
                f"QFrame#taskProfileCard {{ background: {T['BG_PANEL']}; "
                f"border: 1px solid {T['BORDER']}; border-radius: 8px; }}"
                f"QFrame#taskProfileCard QLabel {{ background: transparent; border: none; }}"
            )
            card_layout = QVBoxLayout(profile_card)
            card_layout.setContentsMargins(10, 10, 10, 10)
            card_layout.setSpacing(6)
            title = QLabel(label)
            title.setStyleSheet(f"font-weight: 600; color: {T['TEXT_PRIMARY']};")
            card_layout.addWidget(title)

            profile_settings = ai_settings.get("task_profiles", {}).get(profile_id, {})
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
            selected = profile_settings.get(
                "provider", "ollama" if profile_id == "local_private" else ai_settings["provider"]
            )
            provider_index = provider_combo.findData(selected)
            provider_combo.setCurrentIndex(provider_index if provider_index >= 0 else 0)
            self.task_profile_combos[profile_id] = provider_combo
            provider_combo.setToolTip("Provider used for this task")
            card_layout.addWidget(QLabel("Provider"))
            card_layout.addWidget(provider_combo)

            model_combo = QComboBox()
            model_combo.setEditable(True)
            self.task_profile_model_combos[profile_id] = model_combo
            self._refresh_task_profile_models(
                profile_id, preferred_model=profile_settings.get("model_id")
            )
            provider_combo.currentIndexChanged.connect(
                lambda _index, pid=profile_id: self._on_task_profile_provider_changed(pid)
            )
            card_layout.addWidget(QLabel("Model"))
            card_layout.addWidget(model_combo)
            routing_layout.addWidget(profile_card, 2 + index // 2, index % 2)

        routing_layout.setColumnStretch(0, 1)
        routing_layout.setColumnStretch(1, 1)
        v.addWidget(routing_group)

        behaviour_group = QGroupBox("Behaviour and tools")
        behaviour_layout = QVBoxLayout(behaviour_group)
        behaviour_layout.setContentsMargins(12, 18, 12, 12)
        behaviour_layout.setSpacing(8)
        self.auto_escalation_chk = QCheckBox(
            "Automatically escalate failed/large command explanations to Deep Diagnosis"
        )
        self.auto_escalation_chk.setChecked(bool(ai_settings.get("auto_escalation", False)))
        behaviour_layout.addWidget(self.auto_escalation_chk)
        self.usage_telemetry_chk = QCheckBox(
            "Enable local aggregate AI usage/latency counters (no prompts, logs, or API keys stored)"
        )
        self.usage_telemetry_chk.setChecked(bool(ai_settings.get("usage_telemetry_enabled", False)))
        behaviour_layout.addWidget(self.usage_telemetry_chk)
        self.local_only_ai_chk = QCheckBox(
            "Block all cloud AI requests (use Local / Private inference only)"
        )
        self.local_only_ai_chk.setChecked(not bool(ai_settings.get("cloud_ai_enabled", True)))
        self.local_only_ai_chk.setToolTip(
            "Blocks hosted text-model calls and Google/OpenAI cloud speech services. "
            "Typed Ops Mind requests and local Ollama inference remain available."
        )
        behaviour_layout.addWidget(self.local_only_ai_chk)
        self.ai_intelligence_tools_btn = QPushButton("Open AI Intelligence Workspace…")
        self.ai_intelligence_tools_btn.setToolTip(
            "Open incident-adjacent analysis, cluster comparison, watch planning, "
            "resource recommendations, remediation validation, and service indexing tools."
        )
        self.ai_intelligence_tools_btn.clicked.connect(self._open_ai_intelligence_workspace)
        behaviour_layout.addWidget(self.ai_intelligence_tools_btn)
        v.addWidget(behaviour_group)
        v.addStretch(1)

        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.add_model_btn.clicked.connect(self._add_custom_model)
        self.manage_models_btn.clicked.connect(self._manage_custom_models)
        self.test_model_btn.clicked.connect(self._test_selected_model)
        idx = self.provider_combo.findData(ai_settings["provider"])
        self.provider_combo.setCurrentIndex(idx if idx >= 0 else 0)
        if self._current_provider_id is None:
            self._on_provider_changed(self.provider_combo.currentIndex())

        w.setMaximumWidth(920)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
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
        root = QGridLayout(w)
        root.setContentsMargins(12, 12, 12, 12)
        root.setHorizontalSpacing(14)
        root.setVerticalSpacing(12)

        access_group = QGroupBox("Web App access")
        access_layout = QVBoxLayout(access_group)
        access_layout.setContentsMargins(12, 18, 12, 12)
        access_layout.setSpacing(8)
        self.webapp_enable_btn = QPushButton()
        self.webapp_enable_btn.setCheckable(True)
        self.webapp_enable_btn.setChecked(self._webapp_enabled)
        self.webapp_enable_btn.setMinimumHeight(38)
        self.webapp_enable_btn.clicked.connect(self._on_webapp_toggle)
        access_layout.addWidget(self.webapp_enable_btn)

        self.webapp_status_lbl = QLabel()
        self.webapp_status_lbl.setWordWrap(True)
        self.webapp_status_lbl.setObjectName("secondary_text")
        access_layout.addWidget(self.webapp_status_lbl)
        self._update_webapp_toggle_button(self._webapp_enabled)

        desc = QLabel(
            "The Web App uses a six-digit time-based one-time password that changes "
            "every 30 seconds. The shared seed stays in the OS credential vault."
        )
        desc.setWordWrap(True)
        desc.setObjectName("secondary_text")
        access_layout.addWidget(desc)
        root.addWidget(access_group, 0, 0, Qt.AlignTop)

        otp_group = QGroupBox("Current one-time password")
        otp_layout = QVBoxLayout(otp_group)
        otp_layout.setContentsMargins(12, 18, 12, 12)
        otp_layout.setSpacing(8)
        self.webapp_otp_code = QLabel("------")
        self.webapp_otp_code.setAlignment(Qt.AlignCenter)
        self.webapp_otp_code.setMinimumHeight(52)
        self.webapp_otp_code.setStyleSheet(
            f"color: {T['ACCENT']}; font-size: 27px; font-weight: 600; "
            "letter-spacing: 5px; font-family: monospace;"
        )
        otp_layout.addWidget(self.webapp_otp_code)
        otp_row = QHBoxLayout()
        self.webapp_otp_countdown = QLabel("Remaining: --s")
        self.webapp_otp_countdown.setObjectName("secondary_text")
        otp_row.addWidget(self.webapp_otp_countdown, 1)
        self.webapp_copy_otp_btn = QPushButton("Copy code")
        self.webapp_copy_otp_btn.setEnabled(False)
        self.webapp_copy_otp_btn.clicked.connect(self._copy_webapp_otp)
        otp_row.addWidget(self.webapp_copy_otp_btn)
        otp_layout.addLayout(otp_row)
        root.addWidget(otp_group, 0, 1, Qt.AlignTop)

        remote_group = QGroupBox("Remote access")
        remote_layout = QVBoxLayout(remote_group)
        remote_layout.setContentsMargins(12, 18, 12, 12)
        remote_layout.setSpacing(8)
        warning = QLabel(
            "Remote devices require HTTPS. Set WEBAPP_TLS_CERTFILE and "
            "WEBAPP_TLS_KEYFILE before exposing the Web App."
        )
        warning.setWordWrap(True)
        warning.setObjectName("secondary_text")
        remote_layout.addWidget(warning)
        try:
            from webapp import get_server_url
            login_url = get_server_url()
        except (ImportError, AttributeError):
            login_url = "http://127.0.0.1:8000"
        self.webapp_link = QLabel(f'<a href="{login_url}">Open Web App login</a>')
        self.webapp_link.setOpenExternalLinks(True)
        self.webapp_link.setStyleSheet(f"color: {T['ACCENT']};")
        self.webapp_link.setToolTip(login_url)
        remote_layout.addWidget(self.webapp_link)
        self.webapp_url_label = QLabel(login_url)
        self.webapp_url_label.setWordWrap(True)
        self.webapp_url_label.setObjectName("secondary_text")
        remote_layout.addWidget(self.webapp_url_label)
        root.addWidget(remote_group, 1, 0, 1, 2, Qt.AlignTop)
        root.setColumnStretch(0, 1)
        root.setColumnStretch(1, 1)
        root.setRowStretch(2, 1)

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
            self.webapp_status_lbl.setText(
                "OTP is unavailable. Enable a supported OS credential vault before using Web App login."
            )
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

        performance = {
            "profile": self.performance_profile_combo.currentData(),
            "pause_dashboard_when_inactive": self.pause_dashboard_chk.isChecked(),
            **{key: widget.value() for key, widget in self._performance_values.items()},
        }
        save_settings(
            performance_settings=performance,
            connection_preferences={
                "ssh_connect_timeout_seconds": self.ssh_timeout_spin.value(),
                "ssh_keepalive_seconds": self.ssh_keepalive_spin.value(),
            },
            remember_k8s_context=self.remember_k8s_chk.isChecked(),
            ai_response_detail=self.ai_response_detail_combo.currentData(),
            reduce_motion=self.reduce_motion_chk.isChecked(),
            k8s_hidden_tabs=self.hidden_k8s_tabs(),
        )
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
            cloud_ai_enabled=not self.local_only_ai_chk.isChecked(),
        )
        save_settings(
            webapp_enabled=self.webapp_enable_btn.isChecked(),
            webapp_username="",
            webapp_password="",
            webapp_password_salt="",
            webapp_password_hash="",
        )

        self._webapp_original_enabled = self.webapp_enable_btn.isChecked()
        main_window = self.parent()
        for component_name in ("dashboard_tab", "docker_tab", "k8s_tab"):
            component = getattr(main_window, component_name, None)
            apply_preferences = getattr(component, "apply_refresh_settings", None)
            if callable(apply_preferences):
                try:
                    apply_preferences()
                except Exception:
                    pass
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
