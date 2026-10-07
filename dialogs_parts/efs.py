from .common import *

from efs_manager import EFSException, EFSManager


class EFSConnectDialog(QDialog):
    """AWS EFS connection picker.

    Uses boto3's normal credential/provider chain. No AWS secrets are stored
    by the dialog.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Connect to AWS EFS")
        self.setFixedWidth(520)
        self._manager = None
        self._filesystems = []
        self._regions = []

        apply_qss_to(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(9)

        title = QLabel("AWS EFS")
        title.setFont(QFont("Segoe UI", 16, QFont.Bold))
        title.setStyleSheet(f"color: {T['TEXT_PRIMARY']};")
        layout.addWidget(title)

        info = QLabel(
            "Uses the AWS SDK credential chain — including ~/.aws/credentials, "
            "AWS_PROFILE, environment credentials, and supported instance roles."
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 12px;")
        layout.addWidget(info)

        layout.addWidget(QLabel("AWS Profile (optional)"))
        self.profile_input = QLineEdit()
        self.profile_input.setPlaceholderText("default")
        self.profile_input.setText(os.environ.get("AWS_PROFILE", ""))
        layout.addWidget(self.profile_input)

        region_row = QHBoxLayout()
        region_row.addWidget(QLabel("Region"))
        self.region_input = QComboBox()
        self.region_input.setEditable(True)
        env_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or ""
        if env_region:
            self.region_input.addItem(env_region)
            self.region_input.setCurrentText(env_region)
        else:
            self.region_input.setCurrentText("")
        region_row.addWidget(self.region_input, 1)
        self.load_btn = QPushButton("Load EFS")
        self.load_btn.clicked.connect(self._load_efs)
        region_row.addWidget(self.load_btn)
        layout.addLayout(region_row)

        layout.addWidget(QLabel("EFS Filesystem"))
        self.fs_combo = QComboBox()
        self.fs_combo.setEnabled(False)
        layout.addWidget(self.fs_combo)

        self.identity_label = QLabel("")
        self.identity_label.setWordWrap(True)
        self.identity_label.setStyleSheet(f"color: {T['TEXT_MUTED']}; font-size: 11px;")
        layout.addWidget(self.identity_label)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet(f"color: {T['TEXT_DIM']}; font-size: 11px;")
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Mount & Connect")
        buttons.button(QDialogButtonBox.Ok).setObjectName("primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._buttons = buttons

        if env_region:
            QTimer.singleShot(0, self._load_efs)

    def _manager_for_form(self):
        region = self.region_input.currentText().strip()
        profile = self.profile_input.text().strip() or None
        self._manager = EFSManager(region=region or None, profile=profile)
        return self._manager

    def _load_efs(self):
        try:
            self.load_btn.setEnabled(False)
            self.status_label.setText("Checking AWS credentials and loading EFS…")
            QApplication.processEvents()
            manager = self._manager_for_form()
            identity = manager.caller_identity()
            self.identity_label.setText(
                "AWS identity: {} ({})".format(
                    identity.get("Arn", identity.get("UserId", "unknown")),
                    identity.get("Account", ""),
                )
            )
            self._filesystems = manager.list_filesystems()
            self.fs_combo.clear()
            for fs in self._filesystems:
                label = "{}  •  {}  •  {}".format(
                    fs.name, fs.filesystem_id, fs.state
                )
                self.fs_combo.addItem(label, fs.filesystem_id)
            self.fs_combo.setEnabled(bool(self._filesystems))
            if not self._filesystems:
                self.status_label.setText(
                    "No EFS filesystems are visible with this AWS identity in {}."
                    .format(manager.region)
                )
            else:
                self.status_label.setText(
                    "{} EFS filesystem(s) available in {}.".format(
                        len(self._filesystems), manager.region
                    )
                )
        except Exception as exc:
            self.fs_combo.clear()
            self.fs_combo.setEnabled(False)
            self.status_label.setText("AWS/EFS error: {}".format(exc))
        finally:
            self.load_btn.setEnabled(True)

    def values(self):
        if not self._manager:
            self._manager_for_form()
        filesystem_id = self.fs_combo.currentData()
        if not filesystem_id:
            raise EFSException("Load EFS first and select a filesystem.")
        return self._manager, filesystem_id

    def accept(self):
        try:
            self.values()
        except Exception as exc:
            QMessageBox.warning(self, "AWS EFS", str(exc))
            return
        super().accept()
