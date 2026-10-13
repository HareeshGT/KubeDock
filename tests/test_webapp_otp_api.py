"""Server-side TOTP login/session tests (no production credentials required)."""
import pytest
httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from webapp import server
from webapp.otp_auth import OTPConfigurationError, totp

TEST_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.fixture
def web_client(monkeypatch):
    settings = {"webapp_username": "test-user"}
    monkeypatch.setattr(server, "get_or_create_secret", lambda: TEST_SECRET)
    monkeypatch.setattr(server, "load_settings", lambda: dict(settings))
    def fake_save(**updates):
        settings.update(updates)
    monkeypatch.setattr(server, "save_settings", fake_save)
    monkeypatch.setattr(server, "_client_ip", lambda request: "127.0.0.1")
    server._AUTH_FAILURES.clear()
    server._WEB_SESSIONS.clear()
    with TestClient(server.app, base_url="https://testserver") as client:
        yield client
    server._AUTH_FAILURES.clear()
    server._WEB_SESSIONS.clear()


def test_otp_login_creates_session_and_logout_invalidates_it(web_client):
    code = totp(TEST_SECRET)
    response = web_client.post("/api/login", json={"code": code})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert web_client.get("/api/session").status_code == 200

    assert web_client.post("/api/logout").status_code == 200
    assert web_client.get("/api/session").status_code == 401


def test_invalid_otp_is_rejected_and_password_basic_auth_is_not_a_bypass(web_client):
    response = web_client.post("/api/login", json={"code": "123"})
    assert response.status_code == 401
    assert web_client.get("/api/session", auth=("test-user", "old-password")).status_code == 401


def test_same_time_step_cannot_be_reused(web_client):
    code = totp(TEST_SECRET)
    assert web_client.post("/api/login", json={"code": code}).status_code == 200
    replay = web_client.post("/api/login", json={"code": code})
    assert replay.status_code == 401
    assert "already been used" in replay.json()["detail"]


def test_rate_limit_blocks_repeated_failed_logins(web_client, monkeypatch):
    wrong = str((int(totp(TEST_SECRET)) + 1) % 1000000).zfill(6)
    statuses = [web_client.post("/api/login", json={"code": wrong}).status_code for _ in range(5)]
    assert statuses[-1] == 429


def test_missing_secure_keyring_fails_closed(web_client, monkeypatch):
    monkeypatch.setattr(server, "get_or_create_secret", lambda: (_ for _ in ()).throw(OTPConfigurationError()))
    assert web_client.post("/api/login", json={"code": totp(TEST_SECRET)}).status_code == 503


def test_http_from_remote_client_is_rejected(web_client, monkeypatch):
    monkeypatch.setattr(server, "_client_ip", lambda request: "192.0.2.12")
    response = web_client.post("http://testserver/api/login", json={"code": totp(TEST_SECRET)})
    assert response.status_code == 403
