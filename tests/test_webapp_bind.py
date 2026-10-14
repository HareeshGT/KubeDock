"""Bind-address defaults for the embedded web server."""

import pytest

from webapp import server

_ENV_KEYS = ("WEBAPP_HOST", "WEBAPP_PORT", "WEBAPP_TLS_CERTFILE", "WEBAPP_TLS_KEYFILE")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_default_bind_is_loopback_without_tls():
    assert server._default_bind_host() == "127.0.0.1"
    assert server.get_server_url() == "http://127.0.0.1:8000"


def test_default_bind_is_all_interfaces_with_tls(monkeypatch):
    monkeypatch.setenv("WEBAPP_TLS_CERTFILE", "/tmp/cert.pem")
    monkeypatch.setenv("WEBAPP_TLS_KEYFILE", "/tmp/key.pem")
    assert server._default_bind_host() == "0.0.0.0"
    assert server.get_server_url().startswith("https://")


def test_single_tls_variable_is_not_treated_as_tls(monkeypatch):
    monkeypatch.setenv("WEBAPP_TLS_CERTFILE", "/tmp/cert.pem")
    assert not server._tls_configured()
    assert server._default_bind_host() == "127.0.0.1"


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.5", "::1", "localhost"])
def test_loopback_hosts_detected(host):
    assert server._is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "", None])
def test_non_loopback_hosts_detected(host):
    assert not server._is_loopback_host(host)


def test_explicit_host_override_in_url(monkeypatch):
    monkeypatch.setenv("WEBAPP_HOST", "127.0.0.1")
    monkeypatch.setenv("WEBAPP_PORT", "9001")
    assert server.get_server_url() == "http://127.0.0.1:9001"
