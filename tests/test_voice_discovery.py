import json
import subprocess
from types import SimpleNamespace

from k8s_ai_ops_parts import core, voice


def test_list_voices_uses_bounded_child_process(monkeypatch):
    expected = [
        {"id": "voice.alex", "name": "Alex"},
        {"id": "voice.samantha", "name": "Samantha"},
    ]
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(expected))

    monkeypatch.setattr(core, "_TTS_AVAILABLE", True)
    monkeypatch.setattr(core.subprocess, "run", fake_run)

    assert core.list_voices() == expected
    assert len(calls) == 1
    command, options = calls[0]
    assert command[0] == core.sys.executable
    assert command[1:2] == ["-c"]
    assert options["timeout"] == 8
    assert options["capture_output"] is True
    assert options["text"] is True


def test_list_voices_returns_empty_on_native_voice_timeout(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="pyttsx3 voice discovery", timeout=8)

    monkeypatch.setattr(core, "_TTS_AVAILABLE", True)
    monkeypatch.setattr(core.subprocess, "run", timeout)

    assert core.list_voices() == []


def test_list_voices_returns_empty_when_child_process_fails(monkeypatch):
    monkeypatch.setattr(core, "_TTS_AVAILABLE", True)
    monkeypatch.setattr(
        core.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    assert core.list_voices() == []


def test_list_voices_returns_empty_when_tts_dependency_is_missing(monkeypatch):
    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("voice discovery must not run without pyttsx3")

    monkeypatch.setattr(core, "_TTS_AVAILABLE", False)
    monkeypatch.setattr(core.subprocess, "run", unexpected_run)

    assert core.list_voices() == []



def test_voice_combo_shows_default_before_background_discovery(monkeypatch):
    class FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

        def emit(self):
            for callback in self.callbacks:
                callback()

    class FakeCombo:
        def __init__(self):
            self.items = []
            self.blocked = False

        def blockSignals(self, blocked):
            self.blocked = blocked

        def clear(self):
            self.items.clear()

        def addItem(self, label, value):
            self.items.append((label, value))

        def setCurrentIndex(self, index):
            self.current_index = index

    class FakeWorker:
        def __init__(self):
            self.voices_ready = FakeSignal()
            self.finished = FakeSignal()
            self.started = False

        def isRunning(self):
            return False

        def start(self):
            self.started = True

    class DummyWidget:
        def __init__(self):
            self.voice_combo = FakeCombo()
            self._voice_id = ""
            self._voice_discovery_worker = None

    monkeypatch.setattr(voice, "_TTS_AVAILABLE", True)
    monkeypatch.setattr(voice, "_VoiceDiscoveryWorker", FakeWorker)
    widget = DummyWidget()

    voice.K8sAIOpsVoiceMixin._populate_voice_combo(widget)

    assert widget.voice_combo.items == [("System Default", "")]
    assert widget.voice_combo.current_index == 0
    worker = widget._voice_discovery_worker
    assert worker.started is True

    worker.finished.emit()
    assert widget._voice_discovery_worker is None
    assert worker not in voice._ACTIVE_VOICE_DISCOVERY_WORKERS

def test_list_voices_skips_child_process_in_frozen_app(monkeypatch):
    def unexpected_run(*_args, **_kwargs):
        raise AssertionError(
            "a frozen PyInstaller app must not relaunch its own executable for voice discovery"
        )

    monkeypatch.setattr(core, "_TTS_AVAILABLE", True)
    monkeypatch.setattr(core.sys, "frozen", True, raising=False)
    monkeypatch.setattr(core.subprocess, "run", unexpected_run)

    assert core.list_voices() == []

