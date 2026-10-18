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
    monkeypatch.setattr(server, "_exec", lambda _ssh, command, _timeout, **_kwargs: (calls.append(command) or 0, "ok", ""))
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


def test_resource_kind_allowlist_rejects_command_injection():
    with pytest.raises(HTTPException) as invalid:
        server._resource_kind("pods; id")
    assert invalid.value.status_code == 400


def test_secret_yaml_is_not_exposed(monkeypatch):
    monkeypatch.setattr(server, "_kubectl", lambda *_args, **_kwargs: pytest.fail("secret YAML must not be fetched"))
    with pytest.raises(HTTPException) as denied:
        server.get_kube_resource_yaml("secrets", "default", "app-secret")
    assert denied.value.status_code == 403


def test_apply_yaml_requires_explicit_confirmation(monkeypatch):
    monkeypatch.setattr(server, "_kubectl", lambda *_args, **_kwargs: pytest.fail("unconfirmed YAML must not be applied"))
    with pytest.raises(HTTPException) as denied:
        server.apply_kube_resource(server.ResourceApplyRequest(yaml="apiVersion: v1\nkind: ConfigMap"))
    assert denied.value.status_code == 409


def test_apply_yaml_uses_stdin_and_server_dry_run(monkeypatch):
    calls = []
    audits = []
    monkeypatch.setattr(server, "_kubectl", lambda args, **kwargs: (calls.append((args, kwargs)) or "applied"))
    monkeypatch.setattr(server, "_audit_remote", lambda *args, **kwargs: audits.append((args, kwargs)))
    payload = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: sample\n"
    result = server.apply_kube_resource(server.ResourceApplyRequest(yaml=payload, confirm_apply=True))
    assert result["ok"] is True
    assert len(calls) == 2
    assert calls[0][0].startswith("apply --dry-run=server")
    assert calls[1][0] == "apply -f - 2>&1"
    assert calls[0][1]["input_data"] == payload.encode("utf-8")
    assert calls[1][1]["input_data"] == payload.encode("utf-8")
    assert audits


def test_docker_volume_removal_requires_exact_confirmation(monkeypatch):
    monkeypatch.setattr(server, "_run_remote", lambda *_args, **_kwargs: pytest.fail("unconfirmed removal must not execute"))
    with pytest.raises(HTTPException) as denied:
        server.remote_docker_resource_remove({"kind": "volumes", "identifier": "db-data"})
    assert denied.value.status_code == 409


def test_docker_resource_removal_rejects_unsupported_kind():
    with pytest.raises(HTTPException) as denied:
        server.remote_docker_resource_remove({"kind": "compose", "identifier": "prod"})
    assert denied.value.status_code == 400


def test_process_termination_rejects_mismatched_pid_confirmation(monkeypatch):
    monkeypatch.setattr(server, "_run_remote", lambda *_args, **_kwargs: pytest.fail("must not execute"))
    with pytest.raises(HTTPException) as denied:
        server.remote_terminate_process(server.ProcessTerminateRequest(pid=321, confirm_pid=123))
    assert denied.value.status_code == 409


def test_service_action_requires_exact_confirmation(monkeypatch):
    monkeypatch.setattr(server, "_run_remote", lambda *_args, **_kwargs: pytest.fail("must not execute"))
    with pytest.raises(HTTPException) as denied:
        server.remote_service_action(server.ServiceActionRequest(name="nginx", action="restart", confirm_target="start service/nginx"))
    assert denied.value.status_code == 409


def test_power_action_requires_hostname_and_phrase(monkeypatch):
    monkeypatch.setattr(server, "_remote_os_family", lambda: "linux")
    calls = []
    monkeypatch.setattr(server, "_run_remote", lambda command, timeout=20: (calls.append(command) or 0, "prod-node\n", ""))
    with pytest.raises(HTTPException) as denied:
        server.remote_power_action(server.PowerActionRequest(action="reboot", confirm_target="prod-node", confirm_phrase="REBOOT wrong-node"))
    assert denied.value.status_code == 409
    assert calls == ["hostname"]


def test_service_action_runs_only_allowlisted_verb(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_remote_os_family", lambda: "linux")
    monkeypatch.setattr(server, "_run_remote", lambda command, timeout=20: (calls.append(command) or 0, "ok", ""))
    monkeypatch.setattr(server, "_audit_remote", lambda *_args, **_kwargs: None)
    result = server.remote_service_action(server.ServiceActionRequest(name="nginx", action="restart", confirm_target="restart service/nginx"))
    assert result["ok"] is True
    assert calls == ["systemctl restart nginx"]


def test_remote_path_accepts_absolute_windows_drive_paths():
    assert server._safe_remote_path(r"C:\Users\Administrator") == "C:/Users/Administrator"
    assert server._safe_remote_path("C:\\") == "C:/"
    with pytest.raises(HTTPException):
        server._safe_remote_path(r"C:\Users\..\Windows")


def test_shared_context_switch_requires_explicit_confirmation(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_run_remote", lambda command, timeout=20: (calls.append(command) or 0, "dev\nprod\n", ""))
    with pytest.raises(HTTPException) as denied:
        server.select_kube_context(
            server.ContextSelection(context="prod", scope="shared", confirm_shared_change=False),
            _request(token=""),
        )
    assert denied.value.status_code == 409
    assert calls == ["kubectl config get-contexts -o name 2>&1"]


def test_context_inspection_never_returns_credentials(monkeypatch):
    config = {
        "clusters": [{"name": "prod-cluster", "cluster": {"server": "https://k8s.example:6443"}}],
        "users": [{"name": "prod-user", "user": {"token": "do-not-return-this"}}],
        "contexts": [{"name": "prod", "context": {"cluster": "prod-cluster", "user": "prod-user", "namespace": "production"}}],
    }
    def run(command, timeout=20):
        if "config view" in command:
            import json
            return 0, json.dumps(config), ""
        return 0, "Kubernetes control plane is running\n", ""
    monkeypatch.setattr(server, "_run_remote", run)
    result = server.inspect_kube_context("prod")
    assert result["endpoint"] == "https://k8s.example:6443"
    assert result["auth_methods"] == ["token"]
    assert "do-not-return-this" not in str(result)


def test_context_registration_requires_shared_change_confirmation(monkeypatch):
    monkeypatch.setattr(server, "_run_remote", lambda *_args, **_kwargs: pytest.fail("unconfirmed registration must not execute"))
    with pytest.raises(HTTPException) as denied:
        server.register_kube_context(
            server.ContextRegisterRequest(context="prod-new", cluster="prod-cluster", user="prod-user")
        )
    assert denied.value.status_code == 409


def test_context_delete_blocks_shared_current_context(monkeypatch):
    monkeypatch.setattr(server, "_run_remote", lambda *_args, **_kwargs: (0, "prod\n", ""))
    with pytest.raises(HTTPException) as denied:
        server.delete_kube_context(
            server.ContextDeleteRequest(context="prod", confirm_context="prod", confirm_shared_change=True),
            _request(token=""),
        )
    assert denied.value.status_code == 409


def test_docker_create_rejects_invalid_image_before_execution(monkeypatch):
    monkeypatch.setattr(server, "_run_remote", lambda *_args, **_kwargs: pytest.fail("invalid image must not execute"))
    with pytest.raises(HTTPException) as denied:
        server.remote_docker_create(server.DockerCreateRequest(image="nginx; rm -rf /"))
    assert denied.value.status_code == 400


def test_docker_inspect_redacts_secret_environment_values(monkeypatch):
    import json
    payload = [{"Id": "abc", "Config": {"Env": ["APP_MODE=prod", "DB_PASSWORD=do-not-return", "API_TOKEN=also-secret"]}}]
    monkeypatch.setattr(server, "_run_remote", lambda *_args, **_kwargs: (0, json.dumps(payload), ""))
    result = server.remote_docker_inspect("abc")
    rendered = str(result)
    assert "do-not-return" not in rendered
    assert "also-secret" not in rendered
    assert "APP_MODE=prod" in rendered
    assert result["secrets_redacted"] is True


def test_compose_down_requires_exact_project_confirmation(monkeypatch):
    monkeypatch.setattr(server, "_run_remote", lambda *_args, **_kwargs: pytest.fail("unconfirmed compose down must not execute"))
    with pytest.raises(HTTPException) as denied:
        server.remote_docker_compose_action({"project": "prod", "action": "down"})
    assert denied.value.status_code == 409


def test_file_create_rejects_root_and_move_rejects_same_path(monkeypatch):
    monkeypatch.setattr(server, "_sftp", lambda: pytest.fail("invalid paths must not open SFTP"))
    with pytest.raises(HTTPException) as create:
        server.remote_file_create({"path": "/", "content": "x"})
    assert create.value.status_code == 400
    with pytest.raises(HTTPException) as move:
        server.remote_file_move({"source": "/tmp/a", "destination": "/tmp/a", "confirm_source": "/tmp/a", "confirm_destination": "/tmp/a"})
    assert move.value.status_code == 400


def test_exec_writes_input_to_stdin_without_logging_payload(monkeypatch):
    class Channel:
        def __init__(self):
            self.write_closed = False
        def settimeout(self, _timeout):
            pass
        def recv_exit_status(self):
            return 0
        def shutdown_write(self):
            self.write_closed = True
        def close(self):
            pass

    class Stream:
        def __init__(self, channel, value=b""):
            self.channel = channel
            self.value = value
        def read(self):
            return self.value
        def close(self):
            pass

    class Stdin(Stream):
        def __init__(self, channel):
            super().__init__(channel)
            self.written = b""
        def write(self, value):
            self.written += value
        def flush(self):
            pass

    class SSH:
        def __init__(self):
            self.channel = Channel()
            self.stdin = Stdin(self.channel)
        def exec_command(self, _command, timeout=None):
            return self.stdin, Stream(self.channel, b"ok"), Stream(self.channel, b"")

    monkeypatch.setattr(server, "managed_exec_command", None)
    ssh = SSH()
    code, output, error = server._exec(
        ssh, "kubectl apply -f -", timeout=2, input_data=b"secret yaml", sensitive_output=True
    )
    assert (code, output, error) == (0, "ok", "")
    assert ssh.stdin.written == b"secret yaml"
    assert ssh.channel.write_closed is True


def test_saved_machine_api_never_returns_private_key_path(monkeypatch, tmp_path):
    key_path = tmp_path / "id_ed25519"
    key_path.write_text("placeholder")
    monkeypatch.setattr(server, "load_recent_instances", lambda: [{
        "host": "192.0.2.10", "port": "22", "user": "deploy", "alias": "prod",
        "protocol": "ssh", "pem": str(key_path),
    }])
    monkeypatch.setattr(server, "_desktop_call", lambda callback: callback(object()))
    result = server.desktop_machines()
    assert result["machines"][0]["can_connect"] is True
    assert str(key_path) not in str(result)
