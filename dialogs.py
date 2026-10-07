"""Compatibility facade for KubeDock dialogs.

The implementations are split into focused modules under ``dialogs_parts``
while existing imports such as ``from dialogs import ExecDialog`` remain valid.
"""

from dialogs_parts.transfer import FileTransferDialog
from PyQt5.QtGui import QTextDocument  # compatibility export from the former monolith
from dialogs_parts.connection import MarqueeLabel, _RecentCard, ConnectDialog, ConnectingDialog
from dialogs_parts.efs import EFSConnectDialog
from dialogs_parts.ai_explain import AIExplainDialog
from dialogs_parts.log_viewer import LogViewerDialog
from dialogs_parts.exec_dialog import ContainerPickerDialog, ExecDialog
from dialogs_parts.file_editor import FileEditorDialog
from dialogs_parts.media import MediaPlayerDialog
from dialogs_parts.file_exec import _exec_cmd_for, _ExecStreamWorker, FileExecDialog
from dialogs_parts.search import SearchDialog
from dialogs_parts.tunnel import _IconButton, _ServiceCard, TunnelCard, TunnelCardGrid, _contains_completer, _ServiceFormPanel, ManageTunnelServicesDialog

__all__ = [
    "FileTransferDialog", "MarqueeLabel", "_RecentCard", "ConnectDialog",
    "ConnectingDialog", "EFSConnectDialog", "AIExplainDialog", "LogViewerDialog",
    "ContainerPickerDialog", "ExecDialog", "FileEditorDialog",
    "MediaPlayerDialog", "FileExecDialog", "SearchDialog",
    "ManageTunnelServicesDialog", "TunnelCard", "TunnelCardGrid",
]
