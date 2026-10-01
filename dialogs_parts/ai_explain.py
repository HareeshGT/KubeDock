from .common import *

import re as _re

_FENCE_RE = _re.compile(r"^(\s*)(`{3,}|~{3,})(.*)$")
_ORDERED_RE = _re.compile(r"^(\s*)(\d+)([.)])\s+(.*)$")


def _hoist_list_code_fences(text: str) -> str:
  """Make fenced code blocks that sit inside list items safe for Qt.

  The AI replies put each kubectl command in a fenced block *inside* its
  bullet (``- step`` followed by an indented ```` ``` ```` block). Qt's
  QTextDocument.setMarkdown() mishandles that shape: the code block comes
  out as a bullet of its own and an empty bullet is left behind after it,
  which is the stray ``•`` rows seen in the diagnosis dialog.

  Rewriting every indented fence as a top-level one (dedented, with blank
  lines around it) sidesteps the bug. The bullet before it ends and the
  next bullet starts a fresh list, which renders as: bullet text, code
  block, bullet text, code block. Top-level fences are left untouched.
  Anything inside a fence is copied verbatim apart from the dedent.
  """
  if not text or "```" not in text and "~~~" not in text:
    return text

  out = []
  lines = text.split("\n")
  i = 0
  in_ordered = False     # last list marker seen was "N." / "N)"
  split_ordered = False  # an ordered list has been cut by a hoisted block
  while i < len(lines):
    m = _FENCE_RE.match(lines[i])
    if not m:
      line = lines[i]
      om = _ORDERED_RE.match(line)
      if om:
        in_ordered = True
        if split_ordered and not om.group(1):
          # Qt restarts numbering at 1 for each list piece, so keep the
          # real number as literal text instead of a list marker.
          line = f"{om.group(2)}\\{om.group(3)} {om.group(4)}"
      elif line.lstrip().startswith(("- ", "* ", "+ ")):
        in_ordered = False
      elif line.startswith("#") or line.strip() == "---":
        in_ordered = split_ordered = False
      out.append(line)
      i += 1
      continue

    indent, fence, info = m.group(1), m.group(2), m.group(3)
    # Collect the block up to its closing fence (same char, >= same length).
    j = i + 1
    while j < len(lines):
      cm = _FENCE_RE.match(lines[j])
      if (cm and cm.group(2)[0] == fence[0]
          and len(cm.group(2)) >= len(fence) and not cm.group(3).strip()):
        break
      j += 1
    body = lines[i + 1:j]
    closed = j < len(lines)

    if not indent:
      out.extend(lines[i:j + 1] if closed else lines[i:])
    else:
      n = len(indent.expandtabs(4))

      def _dedent(line, n=n):
        k = 0
        while k < len(line) and k < n and line[k] in " \t":
          k += 1
        return line[k:]

      if in_ordered:
        split_ordered = True
      if out and out[-1].strip():
        out.append("")
      out.append(f"{fence}{info}")
      out.extend(_dedent(b) for b in body)
      out.append(fence)
      out.append("")
    i = j + 1 if closed else len(lines)

  return "\n".join(out)


class AIExplainDialog(QDialog):
  """Interactive AI diagnosis workspace.

  The first response is the original one-shot diagnosis. After it arrives,
  the same dialog becomes a small conversation so the user can ask follow-up
  questions without losing the pod/command evidence that produced the
  diagnosis.
  """

  def __init__(self, parent, title: str, source_context: str = ""):
    super().__init__(parent)
    self.setWindowTitle(title)
    self.resize(760, 620)
    apply_qss_to(self)

    self._source_context = source_context or ""
    self._diagnosis = ""
    self._conversation = []
    self._followup_worker = None
    self._loading_followup = False
    self._followup_enabled = False

    layout = QVBoxLayout(self)
    layout.setContentsMargins(16, 16, 16, 12)
    layout.setSpacing(10)

    header_row = QHBoxLayout()
    header = QLabel(" AI troubleshooting")
    header.setStyleSheet(
      f"color: {T['TEXT_PRIMARY']}; font-size: 15px; font-weight: 700;"
    )
    header_row.addWidget(header)
    header_row.addStretch()
    self.context_lbl = QLabel("Interactive diagnosis")
    self.context_lbl.setStyleSheet(
      f"color: {T['TEXT_MUTED']}; font-size: 11px;"
    )
    header_row.addWidget(self.context_lbl)
    layout.addLayout(header_row)

    self.body = QTextBrowser()
    self.body.setOpenExternalLinks(True)
    # font-family/font-size are set explicitly here (not left to the
    # app-wide QSS) because build_qss() in themes.py has a blanket
    # `QTextEdit { font-family: 'Cascadia Code'...; font-size: 12px; }`
    # rule — QTextBrowser is a QTextEdit subclass, so it inherits that
    # monospace terminal font unless overridden per-widget like this.
    # A widget's own setStyleSheet() takes priority over the app-level
    # one for the properties it sets, so this is enough to opt out.
    ui_font = "'Segoe UI', 'SF Pro Display', 'Inter', Arial, sans-serif"
    self.body.setStyleSheet(
      f"background: {T['BG_ITEM']}; color: {T['TEXT_PRIMARY']}; "
      f"border: 1px solid {T['BORDER']}; border-radius: 8px; padding: 12px; "
      f"font-family: {ui_font}; font-size: 13px;"
    )
    layout.addWidget(self.body, 1)


    quick_row = QHBoxLayout()
    quick_row.setSpacing(6)
    for label, prompt in (
      ("Why?", "Why do you think this is the likely cause?"),
      ("Explain evidence", "Explain the evidence behind the diagnosis."),
      ("What should I check?", "What should I check next?"),
    ):
      btn = QPushButton(label)
      btn.setFixedHeight(30)
      btn.clicked.connect(lambda _=False, p=prompt: self.ask(p))
      quick_row.addWidget(btn)
    quick_row.addStretch()
    layout.addLayout(quick_row)

    ask_row = QHBoxLayout()
    ask_row.setSpacing(8)
    self.question_input = QLineEdit()
    self.question_input.setPlaceholderText(
      "Ask a follow-up about this diagnosis…"
    )
    self.question_input.returnPressed.connect(self._send_followup)
    self.question_input.setEnabled(False)
    ask_row.addWidget(self.question_input, 1)

    self.send_btn = QPushButton("Send")
    self.send_btn.setObjectName("primary")
    self.send_btn.setFixedHeight(34)
    self.send_btn.setEnabled(False)
    self.send_btn.clicked.connect(self._send_followup)
    ask_row.addWidget(self.send_btn)
    layout.addLayout(ask_row)

    btn_row = QHBoxLayout()
    self.copy_btn = QPushButton("Copy conversation")
    self.copy_btn.clicked.connect(
      lambda: QApplication.clipboard().setText(self.body.toPlainText())
    )
    btn_row.addWidget(self.copy_btn)
    btn_row.addStretch()
    close_btn = QPushButton("Close")
    close_btn.setObjectName("primary")
    close_btn.clicked.connect(self.close)
    btn_row.addWidget(close_btn)
    layout.addLayout(btn_row)

    self._loading_timer = QTimer(self)
    self._loading_timer.timeout.connect(self._update_loading_text)
    self._loading_dots = 0

    self._typing_timer = QTimer(self)
    self._typing_timer.timeout.connect(self._type_next_chunk)
    self._response_text = ""
    self._typed_position = 0

    # Separate timer for follow-up responses so their animation does not
    # interfere with the initial diagnosis typing state.
    self._followup_typing_timer = QTimer(self)
    self._followup_typing_timer.timeout.connect(self._type_followup_chunk)
    self._followup_response_text = ""
    self._followup_typed_position = 0

  def set_source_context(self, text: str):
    self._source_context = (text or "").strip()

  def set_loading(self):
    self._typing_timer.stop()
    self._loading_timer.stop()
    self._loading_dots = 0
    self._followup_enabled = False
    self.question_input.setEnabled(False)
    self.send_btn.setEnabled(False)
    self.body.setPlainText("Thinking")
    self._loading_timer.start(400)

  def _update_loading_text(self):
    self._loading_dots = (self._loading_dots + 1) % 4
    self.body.setPlainText(f"Thinking{'.' * self._loading_dots}")

  def stop_loading(self):
    self._loading_timer.stop()

  def set_markdown(self, text: str):
    self.stop_loading()
    self._response_text = text or ""
    self._typed_position = 0
    self.body.clear()
    if not self._response_text:
      self._enable_followups()
      return
    self._typing_timer.start(25)

  def _type_next_chunk(self):
    if self._typed_position >= len(self._response_text):
      self._typing_timer.stop()
      self.body.setMarkdown(_hoist_list_code_fences(self._response_text))
      self._restyle_headings()
      self._diagnosis = self._response_text
      self._conversation = [{"role": "assistant", "content": self._response_text}]
      self._enable_followups()
      return

    next_position = min(self._typed_position + 3, len(self._response_text))
    self.body.setPlainText(self._response_text[:next_position])
    self._typed_position = next_position
    scrollbar = self.body.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())

  def _restyle_headings(self):
    """QTextDocument.setMarkdown() builds heading blocks directly via
    QTextBlockFormat.setHeadingLevel() rather than emitting real HTML
    <h1>-<h6> tags — so document().setDefaultStyleSheet()'s h1/h2/h3
    rules (which only apply to content set via setHtml()) never touch
    them. Headings still come out *slightly* bigger and bold from Qt's
    own built-in heading scale, but not enough to read as a section
    break next to 13px body text. This walks the document after every
    setMarkdown() call and applies size/weight/color/spacing directly
    via QTextCursor, which is the only reliable way to style them.
    """
    doc = self.body.document()
    # Hardcoded rather than read from self.body.font(): the widget's
    # font-size is set via QSS as a *pixel* size (font-size: 13px),
    # which Qt stores as pixelSize — font().pointSizeF() on a
    # pixel-sized font returns -1, not the value it looks like, so
    # deriving base_pt from the widget silently produced ~2pt headings.
    base_pt = 13.0
    heading_sizes = {1: base_pt + 7, 2: base_pt + 5, 3: base_pt + 3}
    accent = QColor(T["ACCENT"])

    block = doc.begin()
    while block.isValid():
      level = block.blockFormat().headingLevel()
      if level:
        next_block = block.next() # grab before this block's format changes

        char_cursor = QTextCursor(block)
        char_cursor.select(QTextCursor.BlockUnderCursor)
        char_fmt = QTextCharFormat()
        char_fmt.setFontPointSize(heading_sizes.get(level, base_pt + 2))
        char_fmt.setFontWeight(QFont.Bold)
        char_fmt.setForeground(accent)
        char_cursor.setCharFormat(char_fmt)

        block_cursor = QTextCursor(block)
        block_fmt = QTextBlockFormat()
        block_fmt.setTopMargin(18)
        block_fmt.setBottomMargin(8)
        block_cursor.setBlockFormat(block_fmt)

        block = next_block
        continue
      block = block.next()

  def _enable_followups(self):
    self._followup_enabled = True
    self.question_input.setEnabled(True)
    self.send_btn.setEnabled(True)
    self.question_input.setFocus()

  def ask(self, question: str):
    if not self._followup_enabled or self._loading_followup:
      return
    self.question_input.setText(question)
    self._send_followup()

  def _send_followup(self):
    if not self._followup_enabled or self._loading_followup:
      return
    question = self.question_input.text().strip()
    if not question:
      return
    self.question_input.clear()
    self._loading_followup = True
    self.question_input.setEnabled(False)
    self.send_btn.setEnabled(False)
    self._conversation.append({"role": "user", "content": question})
    self._render_conversation()
    self._append_thinking()

    provider = ai_assist.get_provider()
    api_key = ai_assist.get_api_key(provider)
    if not api_key:
      self.append_followup_error(
        "No AI API key is configured. Add one in Settings → AI."
      )
      return

    worker = ai_assist.AIConversationWorker(
      provider,
      api_key,
      ai_assist.get_model(provider),
      self._source_context,
      self.conversation_payload()[:-1],
      question,
    )
    worker.done.connect(
      lambda answer, q=question: self.append_followup(q, answer)
    )
    worker.error.connect(self.append_followup_error)
    worker.finished.connect(self._followup_finished)
    self._followup_worker = worker
    worker.start()

  def _append_user(self, text: str):
    safe = html_escape(text).replace("\n", "<br>")
    self.body.append(
      f'<br><div style="margin-top:8px;">'
      f'<span style="color:{T["ACCENT"]}; font-weight:700;">You</span><br>'
      f'<span style="color:{T["TEXT_PRIMARY"]};">{safe}</span></div>'
    )

  def _append_thinking(self):
    self.body.append(
      f'<br><span style="color:{T["TEXT_MUTED"]};">'
      f' Thinking about that…</span>'
    )
    self.body.verticalScrollBar().setValue(self.body.verticalScrollBar().maximum())

  def _render_conversation(self):
    parts = []
    for item in self._conversation:
      role = item.get("role")
      content = str(item.get("content", ""))
      if role == "user":
        parts.append(f"### You\n\n{content}")
      else:
        parts.append(f"### AI\n\n{content}")
    self.body.setMarkdown(_hoist_list_code_fences("\n\n---\n\n".join(parts)))
    self._restyle_headings()
    self.body.verticalScrollBar().setValue(self.body.verticalScrollBar().maximum())

  def append_followup(self, question: str, answer: str):
    """Animate a completed follow-up answer into the conversation."""
    self._followup_typing_timer.stop()
    self._followup_response_text = answer or ""
    self._followup_typed_position = 0

    # Add an empty assistant message; it will be filled progressively.
    self._conversation.append({"role": "assistant", "content": ""})
    self._render_conversation()

    if not self._followup_response_text:
      self._finish_followup_typing()
      return

    self._followup_typing_timer.start(25)

  def _type_followup_chunk(self):
    if not self._conversation:
      self._finish_followup_typing()
      return

    if self._followup_typed_position >= len(self._followup_response_text):
      self._finish_followup_typing()
      return

    next_position = min(self._followup_typed_position + 3,
              len(self._followup_response_text))
    self._followup_typed_position = next_position
    self._conversation[-1]["content"] = self._followup_response_text[:next_position]

    # Plain text during animation avoids expensive Markdown reflow on every tick.
    self.body.setPlainText(self._conversation_to_plain_text())
    scrollbar = self.body.verticalScrollBar()
    scrollbar.setValue(scrollbar.maximum())

  def _conversation_to_plain_text(self):
    parts = []
    for item in self._conversation:
      label = " You" if item.get("role") == "user" else " AI"
      parts.append(f"{label}\n\n{item.get('content', '')}")
    return "\n\n────────────────────────\n\n".join(parts)

  def _finish_followup_typing(self):
    self._followup_typing_timer.stop()
    self._render_conversation()
    self._followup_response_text = ""
    self._followup_typed_position = 0
    self._loading_followup = False
    self.question_input.setEnabled(True)
    self.send_btn.setEnabled(True)
    self.question_input.setFocus()

  def _followup_finished(self):
    """Release the dialog-local worker reference after a follow-up ends."""
    self._followup_worker = None

  def append_followup_error(self, message: str):
    # Keep the conversation intact and show the error as a transient final
    # message; the next question can be asked normally.
    self._render_conversation()
    safe = html_escape(message or "Unknown AI error").replace("\n", "<br>")
    self.body.append(
      f'<br><span style="color:{T["DANGER"]};"> {safe}</span>'
    )
    self._loading_followup = False
    self.question_input.setEnabled(True)
    self.send_btn.setEnabled(True)
    self.question_input.setFocus()

  def conversation_payload(self):
    return list(self._conversation)

  def source_context(self):
    return self._source_context

  def set_error(self, message: str):
    self.stop_loading()
    self._typing_timer.stop()
    self._response_text = ""
    self._typed_position = 0
    self.body.setPlainText(f" {message}")
    self._followup_enabled = False
    self.question_input.setEnabled(False)
    self.send_btn.setEnabled(False)

  def closeEvent(self, event):
    self._loading_timer.stop()
    self._typing_timer.stop()
    self._followup_typing_timer.stop()
    if self._followup_worker is not None:
      try:
        self._followup_worker.quit()
      except Exception:
        pass
    super().closeEvent(event)

