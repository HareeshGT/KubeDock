"""dialogs.py — All modal dialogs: Connect, FileTransfer, LogViewer, Exec,
        FileEditor, FileExec, SearchDialog."""

import base64
import codecs
import io
import os
import re
import shutil
import sys
import tempfile
import shlex
import time
import threading
import sys

# PyQt5 exposes sip as either a top-level package or as PyQt5.sip.
# Some environments have one but not the other, so prefer the bundled
# PyQt5 module and fall back to the top-level import when needed.
try:
  from PyQt5 import sip  # type: ignore[attr-defined]
except Exception:
  try:
    import sip  # type: ignore[import-not-found]
  except ImportError:
    sip = None

from PyQt5.QtWidgets import (
  QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
  QProgressBar, QDialogButtonBox,
  QLineEdit, QFrame, QTextEdit, QFileDialog, QSpinBox, QCheckBox,
  QApplication, QComboBox, QMessageBox, QSplitter, QWidget,
  QPlainTextEdit, QTextBrowser, QAbstractItemView, QTreeWidget,
  QTreeWidgetItem, QHeaderView, QShortcut, QSlider,
  QScrollArea, QStackedWidget, QCompleter, QSizePolicy,
)
from PyQt5.QtCore import Qt, QTimer, pyqtSignal, QObject, QThread, QUrl, QSize, QEvent
from PyQt5.QtGui import QFont, QColor, QTextCursor, QTextCharFormat, QTextBlockFormat, QKeySequence, QIntValidator, QPainter, QFontMetrics

from ui_icons import set_icon, apply_text_icon, icon_button, icon_pixmap
from themes import T, apply_qss_to
from utils import load_recent_instances, size_fmt, append_terminal_html, append_terminal_text, html_escape, monospace_font
from workers import CommandWorker, PodExecStreamWorker, _TransferWorker, ScpTransferWorker, track_worker, FileStreamReadWorker, MediaStreamServer, _StreamServerStartWorker, AudioTranscodeWorker, VideoTranscodeWorker, managed_exec_command, open_managed_session, close_managed_session
from editor_widgets import CodeEditor, make_highlighter, LANG_LABEL
try:
  from monaco_editor import MonacoEditor
except Exception:
  MonacoEditor = None
import ai_assist
from ansi_terminal import AnsiStreamRenderer, plain_text


from themes import rgba as _rgba  # shared helper (themes.rgba)

# QtMultimedia is an optional Qt component — most PyQt5 installs on macOS
# and Linux ship it, but guard the import so a system missing the
# multimedia plugin doesn't break the whole app, just the media player.
try:
  from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
  from PyQt5.QtMultimediaWidgets import QVideoWidget
  _MULTIMEDIA_AVAILABLE = True
except Exception:
  _MULTIMEDIA_AVAILABLE = False


# ─── OS-style file-transfer dialog ───────────────────────────

__all__ = [name for name in globals() if not name.startswith("__")]

# The split dialog modules intentionally use ``from .common import *`` so
# they share the same compatibility namespace as the original monolith.
# Include underscore-prefixed helpers/workers as well; Python's default
# star-import rules would otherwise hide them.
__all__ = [name for name in globals() if not name.startswith('__')]
