"""Background worker for running a bounded AI agent without blocking the Qt UI."""
from __future__ import annotations

from PyQt5.QtCore import QThread, pyqtSignal
from .models import AgentStatus


class AgentRunWorker(QThread):
    done = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, runtime, adapter, request, conversation=None, parent=None):
        """Prepare a single agent turn for execution outside the Qt UI thread."""
        super().__init__(parent)
        self.runtime = runtime
        self.adapter = adapter
        self.request = str(request or "")
        self.conversation = list(conversation or [])
        self.finished.connect(self.deleteLater)

    def run(self):
        """Execute the agent and emit its result or a bounded error message."""
        try:
            result = self.runtime.run(
                self.request, self.adapter, conversation=self.conversation,
            )
            if result.status == AgentStatus.ERROR:
                self.error.emit(result.error or "The AI agent could not complete the request.")
            else:
                self.done.emit(result)
        except Exception as exc:
            self.error.emit(str(exc)[:1000])
