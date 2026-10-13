"""Backend WebApp auth/session regression tests without an HTTP test client."""
import base64
from http.cookies import SimpleCookie

import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from starlette.requests import Request

from webapp import server
from webapp.otp_auth import OTPConfigurationError, check_totp, totp

_SECRET = base64.b32encode(b"webapp-test-secret-123456").decode("ascii").rstrip("=")
_FIXED_TIME = 1_700_000_010.0


def _request(path="/api/login", *, client_ip="127.0.0.1", scheme="http", cookie=None, authorization=None):
    headers = []
    if cookie:
        headers.append((b"cookie", cookie.encode("ascii")))
    if authorization:
        headers.append((b"authorization", authorization.encode("ascii")))
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": (client_ip, 51515),
        "server": ("127.0.0.1", 8000),
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _reset_auth_state(monkeypatch):
    with server._SESSION_LOCK:
        server._WEB_SESSIONS.clear()
    with server._AUTH_FAIL_LOCK:
        server._AUTH_FAILURES.clear()
    monkeypatch.setattr(server, "_log_client_identity", lambda *_args: None)
    monkeypatch.setattr(server, "_auth_block_seconds", lambda _ip: 0)


def _fixed_totp(monkeypatch):
    monkeypatch.setattr(server, "get_or_create_secret", lambda: _SECRET)
    monkeypatch.setattr(
        server,
        "check_totp",
        lambda seed, code, last_accepted_counter=None: check_totp(
            seed, code, timestamp=_FIXED_TIME, last_accepted_counter=last_accepted_counter
        ),
    )
    return totp(_SECRET, _FIXED_TIME)


def test_successful_otp_creates_protected_session_and_replay_is_denied(monkeypatch):
    settings = {"webapp_username": "admin"}
    monkeypatch.setattr(server, "load_settings", lambda: dict(settings))
    monkeypatch.setattr(server, "save_settings", lambda **values: settings.update(values))
    code = _fixed_totp(monkeypatch)

    response = Response()
    result = server.login(server.LoginRequest(code=code), _request(), response)

    assert result["ok"] is True
    assert settings["webapp_totp_last_counter"] == int(_FIXED_TIME // 30)
    assert "httponly" in response.headers["set-cookie"].lower()
    cookie = SimpleCookie()
    cookie.load(response.headers["set-cookie"])
    token = cookie[server._SESSION_COOKIE].value

    authenticated = _request(path="/api/session", cookie=f"{server._SESSION_COOKIE}={token}")
    server.require_login(authenticated)
    assert authenticated.state.auth_user == "admin"

    with pytest.raises(HTTPException) as replay:
        server.login(server.LoginRequest(code=code), _request(), Response())
    assert replay.value.status_code == 401
    assert "already been used" in replay.value.detail.lower()


def test_logout_invalidates_the_session(monkeypatch):
    token, _ttl = server._new_session("admin")
    cookie_value = f"{server._SESSION_COOKIE}={token}"
    server.logout(_request(path="/api/logout", cookie=cookie_value), Response())

    with pytest.raises(HTTPException) as denied:
        server.require_login(_request(path="/api/session", cookie=cookie_value))
    assert denied.value.status_code == 401


def test_basic_auth_alone_never_authenticates(monkeypatch):
    request = _request(authorization="Basic YWRtaW46cGFzc3dvcmQ=")
    with pytest.raises(HTTPException) as denied:
        server.require_login(request)
    assert denied.value.status_code == 401


def test_rate_limit_blocks_before_otp_validation(monkeypatch):
    monkeypatch.setattr(server, "_auth_block_seconds", lambda _ip: 17)
    monkeypatch.setattr(
        server, "get_or_create_secret",
        lambda: pytest.fail("OTP secret should not be read while rate-limited"),
    )
    with pytest.raises(HTTPException) as blocked:
        server.login(server.LoginRequest(code="123456"), _request(), Response())
    assert blocked.value.status_code == 429
    assert blocked.value.headers["Retry-After"] == "17"


def test_missing_secure_keyring_fails_closed(monkeypatch):
    monkeypatch.setattr(server, "get_or_create_secret", lambda: (_ for _ in ()).throw(
        OTPConfigurationError("secure keyring missing")
    ))
    with pytest.raises(HTTPException) as unavailable:
        server.login(server.LoginRequest(code="123456"), _request(), Response())
    assert unavailable.value.status_code == 503


def test_expired_session_is_removed_and_rejected(monkeypatch):
    expired_token = "expired-token"
    with server._SESSION_LOCK:
        server._WEB_SESSIONS[expired_token] = ("admin", server.perf_counter() - 1)
    with pytest.raises(HTTPException) as denied:
        server.require_login(_request(path="/api/session", cookie=f"{server._SESSION_COOKIE}={expired_token}"))
    assert denied.value.status_code == 401
    assert expired_token not in server._WEB_SESSIONS


def test_remote_http_is_rejected_for_page_and_login(monkeypatch):
    with pytest.raises(HTTPException) as denied:
        server._ensure_secure_transport(_request(client_ip="192.168.1.25", scheme="http"))
    assert denied.value.status_code == 403
    server._ensure_secure_transport(_request(client_ip="192.168.1.25", scheme="https"))
