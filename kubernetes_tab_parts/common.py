"""Shared imports for Kubernetes tab mixins.

Kept in one place so the feature mixins can remain focused on behavior.
"""

import json
import re
import shlex
import time
from datetime import datetime, timezone

from PyQt5.QtWidgets import (
  QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
  QComboBox, QLineEdit, QProgressBar, QTabWidget, QTreeWidget,
  QTreeWidgetItem, QListWidget, QListWidgetItem, QTextEdit,
  QSplitter, QFrame, QSpinBox, QHeaderView, QAbstractItemView,
  QDialog, QVBoxLayout as _QVL, QDialogButtonBox, QMessageBox,
  QMenu, QInputDialog, QApplication, QButtonGroup, QCompleter,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QProcess, QSize
from PyQt5.QtGui import QColor, QFont, QFontDatabase

from ui_icons import set_icon, apply_text_icon, add_icon_tab, icon_button, icon_pixmap
from themes import T, apply_qss_to, load_settings, save_settings
from workers import CommandWorker, track_worker
from dialogs import (
  LogViewerDialog, ExecDialog, ManageTunnelServicesDialog, TunnelCardGrid,
  ContainerPickerDialog, AIExplainDialog,
)
import ai_assist
from k8s_ai_ops import K8sAIOpsWidget
from k8s_cards import (
  PodCardWidget, DeploymentCardWidget, ConfigCardWidget,
  ServiceCardWidget, IngressCardWidget,
  StatefulSetCardWidget, DaemonSetCardWidget, EventCardWidget,
  HPACardWidget, PVCCardWidget, PVCardWidget, JobCardWidget,
  CronJobCardWidget,
)
from utils import (
  append_terminal_html, append_terminal_text,
  load_tunnel_services, REMOTE_TUNNEL_CSV_PATH,
  monospace_font,
)

# Preserve the original monolith's shared namespace for all feature mixins,
# including compatibility aliases such as ``_QVL``.
__all__ = [name for name in globals() if not name.startswith('__')]
