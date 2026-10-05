"""Monaco editor bridge for KubeDock's remote file editor.

The visible editor is Monaco (VS Code's editor engine) hosted by Qt WebEngine.
A small QTextDocument shadow model is kept locally so KubeDock's existing
find/replace, save, dirty-state and streaming code can continue to operate
without rewriting the remote-file layer.
"""

from __future__ import annotations

import json
from pathlib import Path

from PyQt5.QtCore import QObject, QUrl, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QTextCursor
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QPlainTextEdit, QLabel

try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView
except Exception:
    QWebEngineView = None


MONACO_VERSION = "0.34.1"


def _language_for(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return {
        ".py": "python", ".pyw": "python",
        ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript",
        ".ts": "typescript", ".tsx": "typescript",
        ".json": "json", ".yaml": "yaml", ".yml": "yaml",
        ".sh": "shell", ".bash": "shell", ".zsh": "shell",
        ".sql": "sql", ".html": "html", ".htm": "html", ".xml": "xml",
        ".css": "css", ".scss": "scss", ".md": "markdown",
        ".go": "go", ".rs": "rust", ".java": "java",
        ".c": "c", ".cpp": "cpp", ".h": "cpp", ".hpp": "cpp",
        ".cs": "csharp", ".swift": "swift", ".kt": "kotlin",
        ".rb": "ruby", ".php": "php",
        ".ini": "ini", ".conf": "ini", ".cfg": "ini", ".toml": "ini",
        ".env": "ini",
    }.get(ext, "plaintext")


class _Bridge(QObject):
    textChanged = pyqtSignal(str)
    saveRequested = pyqtSignal(str)
    cursorChanged = pyqtSignal(int, int)
    selectionChanged = pyqtSignal(str)
    findRequested = pyqtSignal(str)
    editorReady = pyqtSignal()

    @pyqtSlot(str)
    def onTextChanged(self, text):
        self.textChanged.emit(text)

    @pyqtSlot(str)
    def onSaveRequested(self, text):
        self.saveRequested.emit(text)

    @pyqtSlot(int, int)
    def onCursorChanged(self, line, column):
        self.cursorChanged.emit(line, column)

    @pyqtSlot(str)
    def onSelectionChanged(self, text):
        self.selectionChanged.emit(text)

    @pyqtSlot(str)
    def onFindRequested(self, text):
        self.findRequested.emit(text)

    @pyqtSlot()
    def onEditorReady(self):
        self.editorReady.emit()


def _html():
    return """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
html,body,#editor{width:100%;height:100%;margin:0;overflow:hidden;background:#0f1117}
</style>
<script src="https://cdn.jsdelivr.net/npm/monaco-editor@0.34.1/min/vs/loader.js"></script>
<script>
let editor = null;
let decorations = [];
// Python-driven edits (setText/appendText) already update Python's own copy,
// so their change events must not echo the whole document back over the
// QWebChannel: for a streamed file that was one full-document transfer per
// 64KB chunk (O(n^2) bytes, GBs of transient strings for a ~30MB file).
let quiet = 0;
// In large-file mode Python never reads the payload (it pulls text on demand
// via getValue()), so user edits only send a payload-free notification.
let notifyPayload = true;
function boot() {
  if (!window.require) {
    document.getElementById("editor").innerText = "Monaco could not be loaded.";
    return;
  }
  require.config({paths:{vs:"https://cdn.jsdelivr.net/npm/monaco-editor@0.34.1/min/vs"}});
  require(["vs/editor/editor.main"], function(monaco) {
    editor = monaco.editor.create(document.getElementById("editor"), {
      value: window.initialText || "",
      language: window.initialLanguage || "plaintext",
      theme: "vs-dark",
      automaticLayout: true,
      minimap: {enabled:true},
      fontSize: 13,
      fontFamily: "SFMono-Regular, Menlo, Monaco, Consolas, monospace",
      lineNumbers: "on",
      folding: true,
      smoothScrolling: true,
      scrollBeyondLastLine: false,
      renderWhitespace: "selection",
      wordWrap: "on",
      tabSize: 2,
      insertSpaces: true,
      padding: {top:10,bottom:10},
      stickyScroll: {enabled:true},
      cursorBlinking: "smooth",
      bracketPairColorization: {enabled:true},
      guides: {bracketPairs:true},
      suggest: {showMethods:true,showFunctions:true},
      quickSuggestions: true
    });
    editor.onDidChangeModelContent(() => {
      if (quiet || !window._bridge) return;
      _bridge.onTextChanged(notifyPayload ? editor.getValue() : "");
    });
    editor.onDidChangeCursorPosition(e => {
      if (window._bridge) _bridge.onCursorChanged(e.position.lineNumber, e.position.column);
    });
    // Mirror the selected text to Python (capped) so the Qt Find bar can
    // seed its query without going through the clipboard.
    const selText = () => {
      const sel = editor.getSelection();
      const m = editor.getModel();
      if (!sel || !m || sel.isEmpty()) return "";
      return m.getValueInRange(sel).slice(0, 5000);
    };
    editor.onDidChangeCursorSelection(() => {
      if (window._bridge) _bridge.onSelectionChanged(selText());
    });
    // Route Cmd/Ctrl+F to the Qt Find bar instead of Monaco's built-in widget.
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyF, () => {
      if (window._bridge) _bridge.onFindRequested(selText());
    });
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
      if (window._bridge) _bridge.onSaveRequested(editor.getValue());
    });
    // Let Python know the *editor object itself* exists now. loadFinished
    // (the Qt side's signal) only means this HTML page's DOM is ready —
    // Monaco's own bundle is still downloading from the CDN at that point
    // and `editor` is still null for a bit longer. Every window.* call
    // above silently no-ops while `editor` is null, so anything pushed
    // from Python before this fires (e.g. the file's content) is just
    // dropped on the floor rather than queued.
    if (window._bridge) _bridge.onEditorReady();
  });
}
function setText(v) {
  if (!editor) return;
  const p = editor.getPosition();
  quiet++;
  try { editor.setValue(v || ""); } finally { quiet--; }
  if (p) editor.setPosition(p);
}
function appendText(v) {
  // Used while a file is streaming in from the server. Unlike setText()
  // this does NOT replace the model, so it doesn't reset the undo stack,
  // scroll position or (most importantly) any in-flight search
  // decorations on every single chunk of a large file.
  if (!editor || !v) return;
  const model = editor.getModel();
  if (!model) return;
  const lastLine = model.getLineCount();
  const lastCol = model.getLineMaxColumn(lastLine);
  quiet++;
  try {
    model.applyEdits([{
      range: new monaco.Range(lastLine, lastCol, lastLine, lastCol),
      text: v,
      forceMoveMarkers: true
    }]);
  } finally { quiet--; }
}
function setLanguage(v) {
  if (editor) monaco.editor.setModelLanguage(editor.getModel(), v || "plaintext");
}
function setCursor(offset) {
  if (!editor) return;
  const pos = editor.getModel().getPositionAt(offset);
  editor.setPosition(pos);
  editor.revealPositionInCenter(pos);
}
function ensureCursorVisible() {
  if (!editor) return;
  const position = editor.getPosition();
  if (position) {
    editor.revealPositionInCenter(position);
    return;
  }
  editor.revealLineInCenter(1);
}
function setDecorations(ranges) {
  if (!editor) return;
  decorations = editor.deltaDecorations(decorations, ranges.map((r,i)=>({
    range:new monaco.Range(r[0],r[1],r[2],r[3]),
    options:{inlineClassName:i === ranges.length-1 ? "kubedeck-current-match" : "kubedeck-match"}
  })));
}
function clearDecorations() {
  if (editor) decorations = editor.deltaDecorations(decorations, []);
}
function getValue() { return editor ? editor.getValue() : ""; }
function setLargeMode() {
  if (!editor) return;
  notifyPayload = false;
  editor.updateOptions({
    minimap: {enabled:false}, folding:false, wordWrap:"off",
    stickyScroll:{enabled:false}, quickSuggestions:false,
    suggestOnTriggerCharacters:false, parameterHints:{enabled:false},
    bracketPairColorization:{enabled:false}, guides:{bracketPairs:false},
    renderWhitespace:"none", smoothScrolling:false, cursorBlinking:"solid",
    occurrencesHighlight:false, selectionHighlight:false, codeLens:false,
    links:false, unicodeHighlight:{enabled:false}
  });
  monaco.editor.setModelLanguage(editor.getModel(), "plaintext");
}
function setReadOnly(v) {
  if (editor) editor.updateOptions({readOnly:!!v});
}
function undo(){if(editor)editor.trigger("keyboard","undo",null)}
function redo(){if(editor)editor.trigger("keyboard","redo",null)}
function setWrap(v){if(editor)editor.updateOptions({wordWrap:v?"on":"off"})}
function setFontSize(v){if(editor)editor.updateOptions({fontSize:v})}
function focusEditor(){if(editor)editor.focus()}
function disposeEditor(){
  try { if (editor) { editor.setModel(null); editor.dispose(); } } catch(e) {}
  editor = null;
  decorations = [];
}
</script>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<style>
.kubedeck-match{background:rgba(255,190,70,.28);border-bottom:1px solid #e8ad45}
.kubedeck-current-match{background:rgba(124,106,247,.65);color:#fff}
</style>
</head>
<body><div id="editor"></div>
<script>
new QWebChannel(qt.webChannelTransport, function(channel) {
  window._bridge = channel.objects.bridge;
  window.initialText = "";
  window.initialLanguage = "plaintext";
  boot();
});
</script>
</body>
</html>"""


class MonacoEditor(QWidget):
    """VS Code-like Monaco editor with a QTextDocument compatibility model."""

    saveRequested = pyqtSignal(str)
    textChanged = pyqtSignal()
    cursorPositionChanged = pyqtSignal()
    findRequested = pyqtSignal()

    MIN_PT, MAX_PT = 8, 28

    def __init__(self, base_point_size=12, parent=None, large_file=False):
        super().__init__(parent)
        self._pt = base_point_size
        self._base_pt = base_point_size
        self._loaded = False
        self._editor_ready = False
        self._syncing = False
        self._filename = ""
        self._language = "plaintext"
        self._selected = ""
        self._large_file = bool(large_file)
        self._shadow = None if self._large_file else QPlainTextEdit(self)
        if self._shadow is not None:
            self._shadow.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)

        if QWebEngineView is None:
            fallback = QLabel("PyQtWebEngine is not installed.\nUsing the built-in KubeDock editor.")
            layout.addWidget(fallback)
            self._view = None
            return

        from PyQt5.QtWebChannel import QWebChannel
        self._view = QWebEngineView(self)
        self._bridge = _Bridge(self)
        self._bridge.textChanged.connect(self._on_js_text)
        self._bridge.saveRequested.connect(self.saveRequested)
        self._bridge.cursorChanged.connect(self._on_js_cursor)
        self._bridge.selectionChanged.connect(self._on_js_selection)
        self._bridge.findRequested.connect(self._on_js_find)
        self._bridge.editorReady.connect(self._on_editor_ready)

        channel = QWebChannel(self._view.page())
        channel.registerObject("bridge", self._bridge)
        self._view.page().setWebChannel(channel)
        self._channel = channel
        layout.addWidget(self._view)
        self._view.loadFinished.connect(self._on_loaded)
        if self._large_file:
            self._bridge.editorReady.connect(self._enable_large_mode)
        self._view.setHtml(_html(), QUrl("https://kubedeck.local/"))

    @staticmethod
    def available():
        return QWebEngineView is not None

    def _on_loaded(self, ok):
        self._loaded = bool(ok)

    def _enable_large_mode(self):
        if self._view and self._editor_ready:
            self._view.page().runJavaScript("window.setLargeMode()")

    def _on_editor_ready(self):
        self._editor_ready = True
        self._push_state()

    def _on_js_text(self, text):
        if self._syncing:
            return
        if self._large_file:
            self.textChanged.emit()
            return
        self._shadow.blockSignals(True)
        self._shadow.setPlainText(text or "")
        self._shadow.blockSignals(False)
        self.textChanged.emit()

    def _on_js_selection(self, text):
        self._selected = text or ""

    def _on_js_find(self, text):
        self._selected = text or ""
        self.findRequested.emit()

    def selected_text(self):
        """Current selection (single source for Find seeding; no clipboard)."""
        return self._selected

    def _on_js_cursor(self, line, column):
        if self._large_file:
            self.cursorPositionChanged.emit()
            return
        block = self._shadow.document().findBlockByNumber(max(0, line - 1))
        if block.isValid():
            c = self._shadow.textCursor()
            c.setPosition(block.position() + max(0, column - 1))
            self._shadow.setTextCursor(c)
        self.cursorPositionChanged.emit()

    def _push_state(self):
        if not self._view or not self._editor_ready:
            return
        if self._large_file:
            self._view.page().runJavaScript(
                f"window.setLanguage({json.dumps(self._language)});window.setLargeMode();"
            )
            return
        text = json.dumps(self._shadow.toPlainText())
        lang = json.dumps(self._language)
        self._syncing = True
        script = (
            f"window.setText({text});"
            f"window.setLanguage({lang});"
        )
        self._view.page().runJavaScript(script, lambda _=None: self._clear_sync())

    def _clear_sync(self):
        self._syncing = False

    def set_filename(self, filename):
        self._filename = filename or ""
        self._language = _language_for(self._filename)
        self._push_state()

    def setPlainText(self, text):
        if self._large_file:
            if self._view and self._editor_ready:
                self._syncing = True
                self._view.page().runJavaScript(
                    f"window.setText({json.dumps(text or '')});window.setLargeMode();",
                    lambda _=None: self._clear_sync(),
                )
            self.textChanged.emit()
            return
        self._shadow.setPlainText(text or "")
        self._push_state()
        self.textChanged.emit()

    def append_text(self, text):
        if not text:
            return
        if self._large_file:
            if self._view and self._editor_ready:
                self._syncing = True
                self._view.page().runJavaScript(
                    f"window.appendText({json.dumps(text)})",
                    lambda _=None: self._clear_sync(),
                )
            return
        c = self._shadow.textCursor()
        c.movePosition(QTextCursor.End)
        c.insertText(text)
        if self._view and self._editor_ready:
            self._syncing = True
            self._view.page().runJavaScript(
                f"window.appendText({json.dumps(text)})",
                lambda _=None: self._clear_sync(),
            )
        self.textChanged.emit()

    def get_text(self, callback=None):
        if not self._large_file:
            value = self._shadow.toPlainText()
            if callback:
                callback(value)
            return value
        if not self._view or not self._editor_ready:
            if callback:
                callback("")
            return ""
        if callback is None:
            return None
        self._view.page().runJavaScript("window.getValue()", callback)

    def toPlainText(self):
        if self._large_file:
            return ""
        return self._shadow.toPlainText()

    def document(self):
        if self._large_file:
            return None
        return self._shadow.document()

    def textCursor(self):
        if self._large_file:
            return QTextCursor()
        return self._shadow.textCursor()

    def setTextCursor(self, cursor):
        if self._large_file:
            if self._view and self._editor_ready:
                self._view.page().runJavaScript(
                    f"window.setCursor({cursor.position()});window.focusEditor();"
                )
            self.cursorPositionChanged.emit()
            return
        self._shadow.setTextCursor(cursor)
        if self._view and self._editor_ready:
            self._view.page().runJavaScript(
                f"window.setCursor({cursor.position()});window.focusEditor();"
            )
        self.cursorPositionChanged.emit()

    def ensureCursorVisible(self):
        if self._view and self._editor_ready:
            self._view.page().runJavaScript(
                "window.ensureCursorVisible();"
            )
            return
        if self._shadow is not None:
            self._shadow.ensureCursorVisible()

    def replace_selection(self, text):
        if self._large_file:
            return
        cursor = self._shadow.textCursor()
        cursor.insertText(text or "")
        self._push_state()
        self.textChanged.emit()

    def undo(self):
        if self._view and self._editor_ready:
            self._view.page().runJavaScript("window.undo()")
        elif self._shadow is not None:
            self._shadow.undo()

    def redo(self):
        if self._view and self._editor_ready:
            self._view.page().runJavaScript("window.redo()")
        elif self._shadow is not None:
            self._shadow.redo()

    def setUndoRedoEnabled(self, enabled):
        # The dialog turns undo off during the bulk streamed load so the
        # document doesn't keep a second copy of every inserted chunk. This
        # used to be a no-op, leaving the shadow QTextDocument recording each
        # chunk insert (a full extra copy of the file). Monaco owns undo/redo
        # when it is active, so the shadow's own history is never needed.
        if self._shadow is not None:
            self._shadow.setUndoRedoEnabled(bool(enabled) and self._view is None)

    def setLineWrapMode(self, mode):
        wrap = mode != 0
        if self._view and self._editor_ready:
            self._view.page().runJavaScript(f"window.setWrap({str(wrap).lower()})")

    def set_search_selections(self, selections):
        if self._shadow is None:
            return
        ranges = []
        for sel in selections:
            c = sel.cursor
            start = c.selectionStart()
            end = c.selectionEnd()
            a = self._shadow.document().findBlock(start)
            b = self._shadow.document().findBlock(end)
            if not a.isValid() or not b.isValid():
                continue
            ranges.append([
                a.blockNumber()+1, start-a.position()+1,
                b.blockNumber()+1, end-b.position()+1,
            ])
        if self._view and self._editor_ready:
            self._view.page().runJavaScript(
                f"window.setDecorations({json.dumps(ranges)})"
            )

    def zoom_in(self):
        self.set_point_size(self._pt + 1)

    def zoom_out(self):
        self.set_point_size(self._pt - 1)

    def zoom_reset(self):
        self.set_point_size(self._base_pt)

    def set_point_size(self, pt):
        self._pt = max(self.MIN_PT, min(self.MAX_PT, pt))
        if self._view and self._editor_ready:
            self._view.page().runJavaScript(f"window.setFontSize({self._pt})")

    def setFocus(self):
        super().setFocus()
        if self._view and self._editor_ready:
            self._view.page().runJavaScript("window.focusEditor()")

    def dispose(self):
        """Release the Monaco model and WebEngine page resources."""
        view = getattr(self, "_view", None)
        if view is None:
            return
        try:
            view.page().runJavaScript("window.disposeEditor();")
        except Exception:
            pass
        try:
            view.stop()
            view.setHtml("<html><body></body></html>")
        except Exception:
            pass
        try:
            view.deleteLater()
        except Exception:
            pass
        self._view = None
        self._editor_ready = False

    def refresh_theme(self):
        pass
