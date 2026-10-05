"""large_file_viewer.py — LargeFileViewerDialog: a read-only viewer for
remote files too big to load into FileEditorDialog (see
FileEditorDialog.MAX_EDIT_BYTES in dialogs.py).

Why not just raise the cap and stream it in like FileEditorDialog does?
FileEditorDialog's approach — decode the whole file and grow one
QTextDocument (CodeEditor's QPlainTextEdit, or Monaco's shadow document) —
scales with total file size no matter how carefully the streaming is
batched: a bigger file always means a bigger live document, and Qt's rich
text document model costs several times the raw text size in memory. There
is no cap that is "safe" under that design, only a smaller one.

This dialog takes the opposite approach: the file is downloaded to local
disk first (FileTransferDialog.download, reusing the existing transfer
path), then opened with mmap instead of read into a Python str/bytes — the
OS pages it in on demand, so resident memory never scales with file size —
and only ever ONE PAGE_BYTES-sized slice of decoded text is ever put in the
QPlainTextEdit at a time. Paging (rather than infinite/virtual scrolling)
is deliberate: keeping the loaded window's size fixed and swapping it
wholesale on Next/Prev/jump is trivial to get right, whereas a
sliding-window "infinite scroll" viewer has to keep the QPlainTextEdit's
own scroll position in sync with content being added/removed underneath it
on every swap — easy to get subtly wrong, and a real editor with a
scrollbar-jitter bug is worse than a plain pager for what this is: a way
to look at a file that's too big to edit here, not a full viewer
replacement.

The trade-off: this is read-only. A page boundary can also land inside a
multi-byte UTF-8 sequence, which shows as a single replacement character
right at the page edge — cosmetic only, and confined to the boundary.
Editing a 100MB+ file, or exact byte-for-byte text at a page seam, still
means downloading it and using a real local editor.
"""

import mmap
import os
import re
import tempfile

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget, QLabel, QPushButton,
    QLineEdit, QPlainTextEdit, QFrame, QCheckBox, QMessageBox,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QTextCursor

from themes import T, apply_qss_to
from utils import size_fmt, monospace_font
from ui_icons import icon_button, icon_pixmap
from dialogs import FileTransferDialog


from themes import rgba as _rgba  # shared helper (themes.rgba)


class LargeFileViewerDialog(QDialog):
    """Read-only, paged, mmap-backed viewer for one local (downloaded) file."""

    # Kept well under FileEditorDialog.MAX_EDIT_BYTES so a page is cheap
    # for QPlainTextEdit even with no highlighter/undo attached — this is
    # what actually caps memory/CPU use per page, independent of how big
    # the whole file is.
    PAGE_BYTES = 2 * 1024 * 1024

    def __init__(self, parent, remote_path: str, local_path: str):
        super().__init__(parent)
        self._remote = remote_path
        self._local_path = local_path
        self._fh = None
        self._mm = None
        self._page = 0
        self._total_pages = 1

        fname = os.path.basename(remote_path)
        self.setWindowTitle(f"View (read-only) — {fname}")
        self.resize(1180, 760)
        self.setMinimumSize(900, 620)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        apply_qss_to(self)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # ── Header ─────────────────────────────────────────────
        header = QWidget()
        header.setObjectName("viewer_header")
        header.setFixedHeight(64)
        header.setStyleSheet(
            f"QWidget#viewer_header {{ background: {T['BG_PANEL']}; "
            f"border-bottom: 1px solid {T['BORDER']}; }}"
        )
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 8, 16, 8)
        hl.setSpacing(10)

        file_badge = QFrame()
        file_badge.setFixedSize(38, 38)
        file_badge.setStyleSheet(
            f"QFrame {{ background: {_rgba(T['WARNING'], 0.13)}; "
            f"border: 1px solid {_rgba(T['WARNING'], 0.28)}; border-radius: 10px; }}"
        )
        file_badge_lay = QHBoxLayout(file_badge)
        file_badge_lay.setContentsMargins(0, 0, 0, 0)
        file_icon = QLabel()
        file_icon.setPixmap(icon_pixmap("file", color=T["WARNING"], size=19))
        file_icon.setAlignment(Qt.AlignCenter)
        file_badge_lay.addWidget(file_icon)
        hl.addWidget(file_badge)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        title_col.setContentsMargins(0, 1, 0, 1)

        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_lbl = QLabel(fname)
        name_lbl.setStyleSheet(
            f"color: {T['TEXT_PRIMARY']}; font-size: 14px; font-weight: 700; background: transparent;"
        )
        name_row.addWidget(name_lbl)

        readonly_pill = QLabel("READ-ONLY — TOO LARGE TO EDIT")
        readonly_pill.setStyleSheet(
            f"background: {_rgba(T['WARNING'], 0.16)}; color: {T['WARNING']}; "
            f"font-size: 10px; font-weight: 700; border: 1px solid {_rgba(T['WARNING'], 0.35)}; "
            f"border-radius: 8px; padding: 2px 7px;"
        )
        name_row.addWidget(readonly_pill)
        name_row.addStretch(1)
        title_col.addLayout(name_row)

        path_tail = remote_path
        if len(path_tail) > 110:
            path_tail = "…" + path_tail[-109:]
        path_detail = QLabel(path_tail)
        path_detail.setStyleSheet(
            f"color: {T['TEXT_MUTED']}; font-size: 11px; background: transparent;"
        )
        path_detail.setToolTip(remote_path)
        title_col.addWidget(path_detail)

        hl.addLayout(title_col, 1)

        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(32)
        close_btn.setObjectName("viewer_close")
        close_btn.setStyleSheet(
            f"QPushButton#viewer_close {{ background: {T['BG_ITEM']}; color: {T['TEXT_PRIMARY']}; "
            f"border: 1px solid {T['BORDER']}; border-radius: 8px; padding: 0 14px; font-weight: 700; }}"
            f"QPushButton#viewer_close:hover {{ background: {T['BG_ITEM']}; border-color: {T['ACCENT']}; }}"
        )
        close_btn.clicked.connect(self.accept)
        hl.addWidget(close_btn)

        lay.addWidget(header)

        # ── Toolbar: paging + find ────────────────────────────
        tools = QWidget()
        tools.setObjectName("viewer_toolbar")
        tools.setFixedHeight(42)
        tools.setStyleSheet(
            f"QWidget#viewer_toolbar {{ background: {T['BG_DARK']}; border-bottom: 1px solid {T['BORDER']}; }}"
        )
        tl = QHBoxLayout(tools)
        tl.setContentsMargins(12, 5, 12, 5)
        tl.setSpacing(5)

        def _tool_btn(label, tooltip, width=34):
            b = QPushButton(label)
            b.setFixedSize(width, 30)
            b.setToolTip(tooltip)
            b.setStyleSheet(
                f"QPushButton {{ background: transparent; color: {T['TEXT_DIM']}; border: 1px solid transparent; border-radius: 7px; padding: 0 8px; }}"
                f"QPushButton:hover {{ background: {T['BG_ITEM']}; color: {T['TEXT_PRIMARY']}; border-color: {T['BORDER']}; }}"
                f"QPushButton:disabled {{ color: {T['TEXT_MUTED']}; }}"
            )
            return b

        self._first_btn = _tool_btn("⏮", "First page")
        self._first_btn.clicked.connect(lambda: self._go_to_page(0))
        tl.addWidget(self._first_btn)

        self._prev_btn = _tool_btn("◀", "Previous page (Page Up)")
        self._prev_btn.clicked.connect(lambda: self._go_to_page(self._page - 1))
        tl.addWidget(self._prev_btn)

        self._page_lbl = QLabel("Page 1 / 1")
        self._page_lbl.setAlignment(Qt.AlignCenter)
        self._page_lbl.setFixedWidth(120)
        self._page_lbl.setStyleSheet(
            f"color: {T['TEXT_PRIMARY']}; font-size: 12px; font-weight: 600; background: transparent;"
        )
        tl.addWidget(self._page_lbl)

        self._next_btn = _tool_btn("▶", "Next page (Page Down)")
        self._next_btn.clicked.connect(lambda: self._go_to_page(self._page + 1))
        tl.addWidget(self._next_btn)

        self._last_btn = _tool_btn("⏭", "Last page")
        self._last_btn.clicked.connect(lambda: self._go_to_page(self._total_pages - 1))
        tl.addWidget(self._last_btn)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedHeight(20)
        sep.setStyleSheet(f"color: {T['BORDER']};")
        tl.addWidget(sep)

        self._byte_lbl = QLabel("")
        self._byte_lbl.setStyleSheet(
            f"color: {T['TEXT_MUTED']}; font-size: 11px; background: transparent;"
        )
        tl.addWidget(self._byte_lbl)

        tl.addStretch(1)

        self._wrap_chk = QCheckBox("Wrap")
        self._wrap_chk.setStyleSheet(f"QCheckBox {{ color: {T['TEXT_DIM']}; font-size: 11px; spacing: 6px; }}")
        self._wrap_chk.toggled.connect(self._toggle_wrap)
        tl.addWidget(self._wrap_chk)

        self._find_input = QLineEdit()
        self._find_input.setPlaceholderText("Find in file (jumps to the page)…")
        self._find_input.setFixedWidth(240)
        self._find_input.setFixedHeight(28)
        self._find_input.setStyleSheet(
            f"QLineEdit {{ background: {T['BG_ITEM']}; color: {T['TEXT_PRIMARY']}; "
            f"border: 1px solid {T['BORDER']}; border-radius: 7px; padding: 0 8px; }}"
        )
        self._find_input.returnPressed.connect(self._find_next)
        tl.addWidget(self._find_input)

        find_btn = icon_button(" Find")
        find_btn.setFixedHeight(28)
        find_btn.clicked.connect(self._find_next)
        tl.addWidget(find_btn)

        lay.addWidget(tools)

        # ── Editor (read-only) ────────────────────────────────
        self.editor = QPlainTextEdit()
        self.editor.setReadOnly(True)
        self.editor.setUndoRedoEnabled(False)
        self.editor.setFont(monospace_font(12))
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.editor.setStyleSheet(
            f"QPlainTextEdit {{ background: {T['BG_DARK']}; color: {T['TEXT_PRIMARY']}; "
            f"border: none; padding: 10px; }}"
        )
        lay.addWidget(self.editor, 1)

        # ── Footer note ────────────────────────────────────────
        note = QLabel(
            "Viewing a downloaded local copy, one {}-page at a time — nothing here is "
            "read from the remote host, and the whole file is never held in memory at "
            "once. To make changes, download the file and edit it in a local editor."
            .format(size_fmt(self.PAGE_BYTES))
        )
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {T['TEXT_MUTED']}; font-size: 10px; background: {T['BG_PANEL']}; "
            f"border-top: 1px solid {T['BORDER']}; padding: 6px 14px;"
        )
        lay.addWidget(note)

        self._open_mmap()
        self._go_to_page(0)

    # ── mmap / paging ────────────────────────────────────────
    def _open_mmap(self):
        self._fh = open(self._local_path, "rb")
        size = os.fstat(self._fh.fileno()).st_size
        if size == 0:
            self._mm = None
            self._total_pages = 1
            return
        # mmap over the *file*, not a bytes copy — the OS pages this in
        # on demand, so resident memory tracks how much of the file has
        # actually been touched, not the file's total size.
        self._mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        self._total_pages = max(1, (size + self.PAGE_BYTES - 1) // self.PAGE_BYTES)

    def _go_to_page(self, idx: int):
        idx = max(0, min(idx, self._total_pages - 1))
        self._page = idx
        start = idx * self.PAGE_BYTES
        if self._mm is None:
            text = ""
            end = 0
        else:
            end = min(start + self.PAGE_BYTES, len(self._mm))
            # A page edge can fall inside a multi-byte UTF-8 sequence;
            # errors="replace" turns that into a single U+FFFD right at
            # the seam rather than raising, which is the right trade-off
            # for a byte-range viewer over an arbitrary file.
            text = bytes(self._mm[start:end]).decode("utf-8", errors="replace")
        self.editor.setPlainText(text)
        self.editor.moveCursor(QTextCursor.Start)
        self._page_lbl.setText("Page {} / {}".format(self._page + 1, self._total_pages))
        total_size = len(self._mm) if self._mm is not None else 0
        self._byte_lbl.setText(
            "Bytes {} – {} of {}".format(size_fmt(start), size_fmt(end), size_fmt(total_size))
        )
        self._first_btn.setEnabled(self._page > 0)
        self._prev_btn.setEnabled(self._page > 0)
        self._next_btn.setEnabled(self._page < self._total_pages - 1)
        self._last_btn.setEnabled(self._page < self._total_pages - 1)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_PageDown and event.modifiers() == Qt.NoModifier:
            self._go_to_page(self._page + 1)
            return
        if event.key() == Qt.Key_PageUp and event.modifiers() == Qt.NoModifier:
            self._go_to_page(self._page - 1)
            return
        super().keyPressEvent(event)

    def _toggle_wrap(self, on: bool):
        self.editor.setLineWrapMode(QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap)

    # ── Find (byte-level search over the mmap, not the loaded page) ──
    def _find_next(self):
        needle = self._find_input.text()
        if not needle or self._mm is None:
            return
        try:
            pattern = needle.encode("utf-8", errors="ignore")
        except Exception:
            return
        # Search from just after the start of the current page so
        # repeated Enter presses step through matches rather than
        # re-finding the same one.
        search_from = self._page * self.PAGE_BYTES + 1
        idx = self._mm.find(pattern, search_from)
        if idx == -1:
            idx = self._mm.find(pattern, 0)  # wrap around
            if idx == -1:
                QMessageBox.information(self, "Find", f"'{needle}' not found in file.")
                return
        page = idx // self.PAGE_BYTES
        if page != self._page:
            self._go_to_page(page)
        offset_in_page = idx - page * self.PAGE_BYTES
        cur = self.editor.textCursor()
        cur.setPosition(0)
        # Position by character count is an approximation of byte offset
        # when the page contains multi-byte UTF-8 text; close enough to
        # land the viewport on the right area, which is this feature's
        # job — it's a locator, not a precise selection tool.
        cur.setPosition(min(offset_in_page, len(self.editor.toPlainText())))
        self.editor.setTextCursor(cur)
        self.editor.centerCursor()

    # ── Cleanup ──────────────────────────────────────────────
    def closeEvent(self, event):
        self._cleanup()
        super().closeEvent(event)

    def done(self, result):
        self._cleanup()
        super().done(result)

    def _cleanup(self):
        if self._mm is not None:
            try:
                self._mm.close()
            except Exception:
                pass
            self._mm = None
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None
        if self._local_path:
            try:
                os.unlink(self._local_path)
            except Exception:
                pass
            self._local_path = None

    # ── Entry point ──────────────────────────────────────────
    @classmethod
    def open_remote(cls, parent, sftp, remote_path: str,
                     host=None, port=22, user=None, pem=None, password=None,
                     sudo_user=None):
        """Downloads *remote_path* to a local temp file (reusing
        FileTransferDialog's existing progress/cancel UI), then opens it
        in a read-only, paged viewer. Returns None if the download was
        cancelled or failed — nothing to view in that case.
        """
        fname = os.path.basename(remote_path)
        suffix = os.path.splitext(fname)[1]
        fd, local_path = tempfile.mkstemp(prefix="kubedeck-view-", suffix=suffix)
        os.close(fd)

        ok = FileTransferDialog.download(
            parent, sftp, remote_path, local_path,
            host=host, port=port, user=user, pem=pem, password=password,
            sudo_user=sudo_user,
        )
        if not ok:
            try:
                os.unlink(local_path)
            except Exception:
                pass
            return None

        dlg = cls(parent, remote_path, local_path)
        dlg.exec_()
        return dlg