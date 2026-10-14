from __future__ import annotations

from typing import Optional
import os
import re
import stat
import codecs

import paramiko
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QListWidget, QListWidgetItem, QFileDialog, QLineEdit, QLabel,
    QMessageBox, QTextEdit, QSplitter, QStatusBar, QFrame,
    QSizePolicy, QDialog, QDialogButtonBox, QInputDialog, QMenu,
    QAbstractItemView, QProgressBar, QTabWidget, QComboBox,
    QApplication, QShortcut, QGraphicsOpacityEffect, QGraphicsDropShadowEffect,
)
from PyQt5.QtCore import (
    Qt, QSize, QPropertyAnimation, QEasingCurve, QRect, QPoint, QTimer,
    QParallelAnimationGroup, QEvent,
)
from PyQt5.QtGui import QFont, QColor, QPalette, QKeySequence

import themes as _themes
from ui_icons import set_icon, apply_text_icon, add_icon_tab, icon_button, icon_pixmap, split_icon_text
from themes import T, THEMES, apply_theme_vars, build_qss, apply_qss_to, save_settings
from utils import classify, icon_for, size_fmt, add_recent_instance, monospace_font
from sudo_fs import SudoFS
from ftp_fs import FTPFS
from workers import (
    CommandWorker, ConnectWorker, FTPConnectionWorker, ConnectionHealthWorker,
    FileStreamReadWorker, DirectoryListWorker, track_worker, managed_exec_command,
    close_ssh_connection_pool,
)
from dialogs import (
    ConnectDialog, FileTransferDialog, FileEditorDialog, FileExecDialog,
    SearchDialog, ConnectingDialog, MediaPlayerDialog, AIExplainDialog,
)
from large_file_viewer import LargeFileViewerDialog
import ai_assist
from sidebar import Sidebar
from preview import PreviewPane
from file_widgets import FileRowWidget, FileGridWidget
from terminal_widget import TerminalWidget
from kubernetes_tab import KubernetesTab
from dashboard_tab import DashboardTab
from theme_picker import ThemePicker
from settings_dialog import SettingsDialog
from lock_screen import AppLockDialog
import security

_EXECUTABLE_EXTS = {
    ".py", ".sh", ".bash", ".rb", ".js", ".ts", ".php", ".pl",
    ".lua", ".r", ".go", ".java", ".kt", ".rs", ".c", ".cpp", ".swift",
}
_EDITABLE_KINDS = {"text", "code", "key"}
_MEDIA_KINDS = {"video", "audio"}


class FileManagerMixin:
    def _nav_to(self, path):
      if not self.sftp:
        return
      try:
        resolved = self.sftp.normalize(path or "")
      except Exception as e:
        QMessageBox.warning(self, "Navigation Error", str(e))
        return
      if self.current_path and self.current_path != resolved:
        self.history.append(self.current_path)
        self.future.clear()
      self.current_path = resolved
      if self._conn_protocol in ("ftp", "ftps") and hasattr(self, "dashboard_tab"):
        self.dashboard_tab.set_ftp_path(resolved)
      self._refresh()

    def _go_back(self):
      if self.history:
        self.future.append(self.current_path)
        self.current_path = self.history.pop()
        self._refresh(push_history=False)

    def _go_forward(self):
      if self.future:
        self.history.append(self.current_path)
        self.current_path = self.future.pop()
        self._refresh(push_history=False)

    def _go_up(self):
      parent = os.path.dirname(self.current_path) or "/"
      if parent != self.current_path:
        self._nav_to(parent)

    def _navigate_addr(self):
      self._nav_to(self.addr_bar.text().strip())

    def _refresh(self, push_history=True):
      if not self.sftp:
        return

      if self._directory_worker is not None and self._directory_worker.isRunning():
        self._directory_generation += 1
        self._directory_worker.stop()
        self._directory_refresh_pending = (self.current_path, push_history)
        return

      self._directory_refresh_pending = None
      self._directory_generation += 1
      self._row_population_generation += 1
      generation = self._directory_generation
      path = self.current_path

      self.progress.show()
      self.file_list.setEnabled(False)
      self.file_list.clear()
      self._items = []
      self.preview.clear()
      self.search_bar.clear()

      worker = DirectoryListWorker(self.sftp, path)
      self._directory_worker = worker
      track_worker(self._workers, worker)
      worker.result.connect(
        lambda metas, result_path, w=worker, g=generation:
          self._on_directory_loaded(metas, result_path, w, g)
      )
      worker.error.connect(
        lambda error, w=worker, g=generation:
          self._on_directory_error(error, w, g)
      )
      worker.finished.connect(
        lambda w=worker: self._on_directory_worker_finished(w)
      )
      worker.start()

    def _on_directory_loaded(self, metas, path, worker, generation):
      if generation != self._directory_generation:
        return
      if worker is not self._directory_worker:
        return
      if self.sftp is None or path != self.current_path:
        return

      items = []
      for meta in metas:
        mode_value = meta.get("mode", 0) or 0
        name = meta.get("name", "")
        is_dir = bool(meta.get("is_dir"))
        items.append({
          "name": name,
          "kind": classify(name, is_dir, mode_value),
          "size": meta.get("size", 0) or 0,
          "mode": oct(mode_value)[-3:] if mode_value else "---",
          "is_dir": is_dir,
        })

      self._items = self._sort_items(items)
      self._row_population_generation += 1
      population_generation = self._row_population_generation
      self._populate_rows_chunk(0, path, population_generation)

    def _populate_rows_chunk(self, start, path, generation):
      if generation != self._row_population_generation:
        return
      if self.sftp is None or path != self.current_path:
        return

      batch_size = 100
      end = min(start + batch_size, len(self._items))
      for meta in self._items[start:end]:
        self._add_row(meta)

      if end < len(self._items):
        QTimer.singleShot(
          0, lambda e=end, p=path, g=generation:
            self._populate_rows_chunk(e, p, g)
        )
        return

      self.file_list.setEnabled(True)
      self.progress.hide()
      self.addr_bar.setText(self.current_path)
      self.setWindowTitle("KubeDock — {}".format(self.current_path))
      n = len(self._items)
      self.status.showMessage(
        "{} item{} in {}".format(n, "s" if n != 1 else "", self.current_path)
      )

    def _on_directory_error(self, error, worker, generation):
      if generation != self._directory_generation or worker is not self._directory_worker:
        return
      if self.sftp is None:
        return
      msg = str(error)
      if self._sudo_user:
        msg += "\n\n(Running as sudo user '{}')".format(self._sudo_user)
      self.file_list.setEnabled(True)
      self.progress.hide()
      QMessageBox.critical(self, "Error", msg)

    def _on_directory_worker_finished(self, worker):
      if worker is not self._directory_worker:
        return
      self._directory_worker = None
      pending = self._directory_refresh_pending
      self._directory_refresh_pending = None
      if pending and self.sftp is not None:
        path, push_history = pending
        if path == self.current_path:
          self._refresh(push_history=push_history)

    def _sort_items(self, items):
      key_func = self.SORT_KEY_FUNCS.get(self.sort_key, self.SORT_KEY_FUNCS["Name"])
      items = sorted(items, key=key_func, reverse=self.sort_reverse)
      items = sorted(items, key=lambda m: not m["is_dir"])
      return items

    def _relist(self):
      self.file_list.clear()
      for meta in self._items:
        self._add_row(meta)

    def _add_row(self, meta):
      item = QListWidgetItem()
      item.setData(Qt.UserRole, meta)
      if self.view_mode == "grid":
        item.setSizeHint(QSize(FileGridWidget.TILE_W, FileGridWidget.TILE_H))
        self.file_list.addItem(item)
        self.file_list.setItemWidget(item, FileGridWidget(meta))
      else:
        item.setSizeHint(QSize(0, 36))
        self.file_list.addItem(item)
        self.file_list.setItemWidget(item, FileRowWidget(meta))

    def _filter_list(self, text):
      q = text.lower()
      self.file_list.clear()
      for meta in self._items:
        if q in meta["name"].lower():
          self._add_row(meta)

    def _change_sort_key(self, key):
      self.sort_key = key
      self._items  = self._sort_items(self._items)
      self._relist()
      self.status.showMessage("Sorted by {}".format(key))

    def _toggle_sort_direction(self):
      self.sort_reverse = not self.sort_reverse
      self.sort_dir_btn.setText("↓" if self.sort_reverse else "↑")
      self._items = self._sort_items(self._items)
      self._relist()

    def _set_view_mode(self, mode):
      self.view_mode = mode
      self.view_list_btn.setChecked(mode == "list")
      self.view_grid_btn.setChecked(mode == "grid")
      if mode == "grid":
        self.file_list.setViewMode(QListWidget.IconMode)
        self.file_list.setFlow(QListWidget.LeftToRight)
        self.file_list.setWrapping(True)
        self.file_list.setResizeMode(QListWidget.Adjust)
        self.file_list.setMovement(QListWidget.Static)
        self.file_list.setSpacing(10)
        self.file_list.setGridSize(QSize(FileGridWidget.TILE_W + 14, FileGridWidget.TILE_H + 14))
        self.list_header.hide()
      else:
        self.file_list.setViewMode(QListWidget.ListMode)
        self.file_list.setFlow(QListWidget.TopToBottom)
        self.file_list.setWrapping(False)
        self.file_list.setResizeMode(QListWidget.Adjust)
        self.file_list.setMovement(QListWidget.Static)
        self.file_list.setSpacing(0)
        self.file_list.setGridSize(QSize())
        self.list_header.show()
      self._relist()
      self.status.showMessage("{} view".format(mode.capitalize()))

    def _on_click(self, item):
      meta = item.data(Qt.UserRole)
      if not meta:
        return
      self.preview.show_entry(meta["name"], meta["kind"], meta["size"], meta["mode"])
      if meta["kind"] in ("text", "code") and not meta["is_dir"]:
        self._fetch_preview(meta["name"])

    def _on_double_click(self, item):
      meta = item.data(Qt.UserRole)
      if not meta:
        return
      if meta["is_dir"]:
        self._nav_to(self.current_path.rstrip("/") + "/" + meta["name"])
      else:
        self._open_file(meta)

    def _open_file(self, meta):
      """Double-click handler: edit text/code, offer Run for scripts, download otherwise."""
      kind = meta["kind"]
      name = meta["name"]
      ext = os.path.splitext(name)[1].lower()

      if kind in _MEDIA_KINDS:
        self._play_media(meta)
      elif kind in _EDITABLE_KINDS:
        self._edit_file(meta)
      elif ext in _EXECUTABLE_EXTS:
        self._exec_file(meta)
      else:
        save, _ = QFileDialog.getSaveFileName(self, "Save File", name)
        if save:
          remote = self.current_path.rstrip("/") + "/" + name
          FileTransferDialog.download(
            self, self.sftp, remote, save,
            host=self._conn_host, port=self._conn_port,
            user=self._conn_user, pem=self._conn_pem,
            password=self._conn_password,
            sudo_user=self._sudo_user,
          )
          self.status.showMessage("Downloaded {}".format(name))

    def _cancel_preview_worker(self):
      """Stop any in-flight preview read before starting a second read of
      the same (or a different) remote file.

      _fetch_preview runs on a background FileStreamReadWorker (see its
      docstring), so a single click's preview fetch can still be running
      when Edit/Play/Run is triggered a moment later — whether via
      double-click (_open_file), the toolbar buttons (_edit_selected /
      _run_selected), or a context-menu action. All of those funnel
      through _edit_file/_play_media/_exec_file, so the guard lives there
      rather than only in _open_file — a previous version of this guard
      only covered the double-click path and missed the toolbar-button
      route entirely. Two FileStreamReadWorkers hitting the sudo-cat
      raw-channel path concurrently on the same SSH transport can desync
      the packet stream ("Garbage packet received").
      """
      if self._preview_worker is not None:
        worker = self._preview_worker
        self._preview_worker = None
        try:
          worker.cancel()
        except RuntimeError:
          # Qt may have already destroyed the Python wrapper after the
          # worker finished. Treat that as already cancelled.
          pass
        # Nothing else should move the pane off the previous preview's
        # loading state after cancellation.
        self.preview.reset_text()

    def _selected_meta(self):
      item = self.file_list.currentItem()
      if not item:
        return None
      return item.data(Qt.UserRole)

    def _edit_file(self, meta):
      if not self.sftp:
        return
      self._cancel_preview_worker()
      remote = self._current_remote(meta)
      # Files over FileEditorDialog's own load cap never get to the point
      # of streaming into it — the editor's cost scales with how much of
      # the file it ends up holding live in one QTextDocument, so there's
      # no cap on that path that's actually "safe", only a smaller one.
      # Route these to the read-only, mmap-backed pager instead, whose
      # memory/CPU cost is bounded by one page, independent of file size.
      if meta.get("size", 0) > FileEditorDialog.MAX_EDIT_BYTES:
        LargeFileViewerDialog.open_remote(
          self, self.sftp, remote,
          host=self._conn_host, port=self._conn_port,
          user=self._conn_user, pem=self._conn_pem,
          password=self._conn_password,
          sudo_user=self._sudo_user,
        )
        return
      FileEditorDialog.open_remote(
        self, self.sftp, self.ssh, remote,
        sudo_user=self._sudo_user,
      )

    def _edit_selected(self):
      meta = self._selected_meta()
      if not meta:
        QMessageBox.information(self, "Edit", "Select a file first.")
        return
      if meta["is_dir"]:
        QMessageBox.information(self, "Edit", "Cannot edit a directory.")
        return
      self._edit_file(meta)

    def _play_media(self, meta):
      if not self.sftp:
        return
      self._cancel_preview_worker()
      remote = self._current_remote(meta)
      MediaPlayerDialog.open_remote(
        self, self.sftp, self.ssh, remote, meta["kind"], sudo_user=self._sudo_user
      )

    def _exec_file(self, meta):
      if not self.ssh:
        return
      self._cancel_preview_worker()
      remote = self._current_remote(meta)
      dlg = FileExecDialog(self, self.ssh, remote, sudo_user=self._sudo_user)
      dlg.exec_()

    def _run_selected(self):
      meta = self._selected_meta()
      if not meta:
        QMessageBox.information(self, "Run", "Select a file first.")
        return
      if meta["is_dir"]:
        QMessageBox.information(self, "Run", "Cannot run a directory.")
        return
      ext = os.path.splitext(meta["name"])[1].lower()
      if ext not in _EXECUTABLE_EXTS and meta["kind"] not in ("exec",):
        QMessageBox.information(
          self, "Run",
          f"'{meta['name']}' doesn't have a known executable extension.\n"
          "You can still open the Run dialog and choose a custom interpreter."
        )
      self._exec_file(meta)

    def _fetch_preview(self, name, show_dialog=False):
      """Loads (up to) the first 32KB of a remote file for the preview
      pane or the 'View' dialog. Runs via FileStreamReadWorker on a
      background QThread rather than the old direct
      `self.sftp.open(remote, "r"); f.read(32768)` — that blocked the UI
      thread on every click, and worse, under an active sudo user
      SudoFS.open() pulls the *entire* remote file into memory via `cat`
      before the 32KB read() cap ever applied, which is what turned
      clicking a large file into a hang/crash rather than a bounded
      preview. See FileStreamReadWorker's docstring in workers.py for
      the full history — FileEditorDialog was already moved onto it;
      this was the one remaining caller still using the old pattern.
      """
      remote = self.current_path.rstrip("/") + "/" + name

      # Cancel any preview still loading so rapidly clicking through
      # several files doesn't leave stale background workers racing to
      # overwrite the (by-then-different) preview target.
      self._cancel_preview_worker()

      decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
      state = {"text": ""}

      dlg = te = None
      if show_dialog:
        dlg = QDialog(self)
        dlg.setWindowTitle("View: {}".format(name))
        dlg.resize(760, 560)
        apply_qss_to(dlg)
        lay = QVBoxLayout(dlg)
        te = QTextEdit()
        te.setReadOnly(True)
        te.setFont(monospace_font(11))
        te.setPlainText("Loading…")
        lay.addWidget(te)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
      else:
        self.preview.show_text("Loading…")

      def _on_chunk(chunk: bytes):
        text = decoder.decode(chunk)
        if not text:
          return
        state["text"] += text
        if te is not None:
          te.setPlainText(state["text"])
        else:
          self.preview.show_text(state["text"])

      def _on_finished(_total_bytes):
        decoder.decode(b"", final=True)
        self._preview_worker = None

      def _on_error(_err):
        message = "(binary or unreadable file)"
        if te is not None:
          te.setPlainText(message)
        else:
          self.preview.show_text(message)
        self._preview_worker = None

      worker = FileStreamReadWorker(
        self.sftp, self.ssh, remote,
        sudo_user=self._sudo_user, max_bytes=32768,
      )
      worker.chunk_ready.connect(_on_chunk)
      worker.finished_ok.connect(_on_finished)
      worker.finished_err.connect(_on_error)
      self._preview_worker = worker
      worker.start()

      if dlg is not None:
        # Modal, but Qt's own event loop underneath exec_() keeps
        # processing the worker's queued signals, so chunks still
        # stream into `te` live while the dialog is open.
        dlg.finished.connect(lambda _: worker.cancel())
        dlg.exec_()

    def _current_remote(self, meta):
      return self.current_path.rstrip("/") + "/" + meta["name"]

    def _upload(self):
      if not self.sftp:
        return
      paths, _ = QFileDialog.getOpenFileNames(self, "Select Files to Upload")
      if not paths:
        return
      errors = []
      for local in paths:
        remote = self.current_path.rstrip("/") + "/" + os.path.basename(local)
        ok = FileTransferDialog.upload(
          self, self.sftp, local, remote,
          host=self._conn_host, port=self._conn_port,
          user=self._conn_user, pem=self._conn_pem,
          password=self._conn_password,
          sudo_user=self._sudo_user,
        )
        if not ok:
          errors.append(os.path.basename(local))
      if errors:
        QMessageBox.warning(self, "Upload Cancelled/Failed",
                  "These files were not fully uploaded:\n" + "\n".join(errors))
      self._refresh()
      self.status.showMessage("Uploaded {} file(s)".format(len(paths) - len(errors)))

    def _download(self):
      item = self.file_list.currentItem()
      if not item:
        return
      meta = item.data(Qt.UserRole)
      if not meta or meta["is_dir"]:
        QMessageBox.information(self, "Download", "Select a file (not a folder) to download.")
        return
      default_path = os.path.join(
        os.path.expanduser("~"), "Downloads", meta["name"]
      )
      save, _ = QFileDialog.getSaveFileName(self, "Save As", default_path)
      if not save:
        return
      FileTransferDialog.download(
        self, self.sftp, self._current_remote(meta), save,
        host=self._conn_host, port=self._conn_port,
        user=self._conn_user, pem=self._conn_pem,
        password=self._conn_password,
        sudo_user=self._sudo_user,
      )
      self.status.showMessage("Saved to {}".format(save))

    def _new_folder(self):
      if not self.sftp:
        return
      name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
      if not ok or not name.strip():
        return
      try:
        self.sftp.mkdir(self.current_path.rstrip("/") + "/" + name.strip())
        self._refresh()
      except Exception as e:
        QMessageBox.critical(self, "Error", str(e))

    def _new_file(self):
      if not self.sftp:
        return
      name, ok = QInputDialog.getText(self, "New File", "File name:")
      if not ok or not name.strip():
        return
      try:
        remote = self.current_path.rstrip("/") + "/" + name.strip()
        if hasattr(self.sftp, "write_bytes"):
          self.sftp.write_bytes(remote, b"")
        else:
          self.sftp.open(remote, "w").close()
        self._refresh()
      except Exception as e:
        QMessageBox.critical(self, "Error", str(e))

    def _delete(self):
      item = self.file_list.currentItem()
      if not item:
        return
      meta = item.data(Qt.UserRole)
      if not meta:
        return
      kind_str = "folder" if meta["is_dir"] else "file"
      if QMessageBox.question(self, "Delete", 'Delete {} "{}"?'.format(kind_str, meta["name"]),
                  QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
        return
      try:
        remote = self._current_remote(meta)
        if meta["is_dir"]:
          self.sftp.rmdir(remote)
        else:
          self.sftp.remove(remote)
        self._refresh()
      except Exception as e:
        QMessageBox.critical(self, "Delete Error", str(e))

    def _rename_selected(self):
      item = self.file_list.currentItem()
      if not item:
        QMessageBox.information(self, "Rename", "Select a file or folder first.")
        return
      meta = item.data(Qt.UserRole)
      if not meta:
        return
      self._rename_meta(meta)

    def _rename_meta(self, meta):
      old_name = meta["name"]
      kind_str = "folder" if meta["is_dir"] else "file"
      new_name, ok = QInputDialog.getText(
        self, "Rename {}".format(kind_str.capitalize()),
        "New name:", text=old_name
      )
      if not ok:
        return
      new_name = new_name.strip()
      if not new_name or new_name == old_name:
        return
      if "/" in new_name:
        QMessageBox.warning(self, "Rename", "Name cannot contain '/'.")
        return

      old_remote = self._current_remote(meta)
      new_remote = self.current_path.rstrip("/") + "/" + new_name

      # Avoid silently clobbering an existing file/folder with that name.
      existing_names = {m["name"] for m in self._items}
      if new_name in existing_names:
        if QMessageBox.question(
          self, "Rename",
          'An item named "{}" already exists here. Overwrite it?'.format(new_name),
          QMessageBox.Yes | QMessageBox.No
        ) != QMessageBox.Yes:
          return

      try:
        self.sftp.rename(old_remote, new_remote)
        self._refresh()
        self.status.showMessage('Renamed "{}" to "{}"'.format(old_name, new_name))
      except Exception as e:
        QMessageBox.critical(self, "Rename Error", str(e))

