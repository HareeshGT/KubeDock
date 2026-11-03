import json
import subprocess
from types import SimpleNamespace

from k8s_ai_ops_parts import core


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
