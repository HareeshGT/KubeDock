"""Regression tests for the KubeDock remote-control API helpers."""
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from webapp import server


def _request(path="/api/kube/contexts/select", token=""):
    headers = []
    if token:
        headers.append((b"cookie", f"{server._SESSION_COOKIE}={token}".encode("ascii")))
    return Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    })


def test_remote_path_requires_absolute_path_and_rejects_traversal():
    assert server._safe_remote_path("/var/log/kubedock") == "/var/log/kubedock"
    with pytest.raises(HTTPException) as relative:
        server._safe_remote_path("etc/passwd")
    assert relative.value.status_code == 400
    with pytest.raises(HTTPException) as traversal:
        server._safe_remote_path("/var/../etc/passwd")
    assert traversal.value.status_code == 400
    with pytest.raises(HTTPException):
        server._safe_remote_path("/tmp/\x00bad")


def test_context_selection_is_session_scoped_and_validated(monkeypatch):
    token, _ = server._new_session("test-user")
    monkeypatch.setattr(server, "_run_remote", lambda *_args, **_kwargs: (0, "dev\nprod\n", ""))
    monkeypatch.setattr(server, "_audit_remote", lambda *_args, **_kwargs: None)
    try:
        result = server.select_kube_context(
            server.ContextSelection(context="prod"),
            _request(token=token),
        )
        assert result["ok"] is True
        assert result["scope"] == "web-session"
        assert server._SESSION_CONTEXTS[token] == "prod"

        reset = server.select_kube_context(
            server.ContextSelection(context=""),
            _request(token=token),
        )
        assert reset["context"] is None
        assert token not in server._SESSION_CONTEXTS

        with pytest.raises(HTTPException) as missing:
            server.select_kube_context(
                server.ContextSelection(context="unknown"),
                _request(token=token),
            )
        assert missing.value.status_code == 404
    finally:
        with server._SESSION_LOCK:
            server._WEB_SESSIONS.pop(token, None)
            server._SESSION_CONTEXTS.pop(token, None)


def test_kubectl_uses_selected_context_without_mutating_kubeconfig(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_ssh", lambda: object())
    monkeypatch.setattr(server, "_exec", lambda _ssh, command, _timeout: (calls.append(command) or 0, "ok", ""))
    context_token = server._KUBE_CONTEXT.set("prod-context")
    try:
        assert server._kubectl("get pods") == "ok"
        assert calls == ["kubectl --context prod-context get pods"]
        assert server._kubectl("config get-contexts -o name") == "ok"
        assert calls[-1] == "kubectl config get-contexts -o name"
    finally:
        server._KUBE_CONTEXT.reset(context_token)


def test_docker_action_rejects_unsupported_actions_before_execution(monkeypatch):
    monkeypatch.setattr(server, "_run_remote", lambda *_args, **_kwargs: pytest.fail("must not execute"))
    with pytest.raises(HTTPException) as invalid:
        server.remote_docker_action("container123", "exec", {})
    assert invalid.value.status_code == 400


def test_docker_remove_requires_exact_confirmation(monkeypatch):
    monkeypatch.setattr(server, "_run_remote", lambda *_args, **_kwargs: pytest.fail("must not execute"))
    with pytest.raises(HTTPException) as denied:
        server.remote_docker_action("container123", "remove", {})
    assert denied.value.status_code == 409


def test_system_inspection_uses_detected_windows_command(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_remote_os_family", lambda: "windows")
    monkeypatch.setattr(server, "_run_remote", lambda command, timeout=20: (calls.append(command) or 0, "PowerShell output", ""))
    monkeypatch.setattr(server, "_audit_remote", lambda *_args, **_kwargs: None)
    result = server.remote_system_inspection("processes")
    assert result["os"] == "windows"
    assert "Get-Process" in calls[0]
