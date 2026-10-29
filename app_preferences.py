"""Shared, validated preferences for KubeDock performance and connections."""
from __future__ import annotations
import hashlib
import json

PERFORMANCE_PROFILES = {
    "low_resource": {"dashboard_refresh_seconds": 5, "background_refresh_seconds": 120, "kubernetes_refresh_seconds": 60, "docker_refresh_seconds": 15, "tunnel_refresh_seconds": 30},
    "balanced": {"dashboard_refresh_seconds": 3, "background_refresh_seconds": 60, "kubernetes_refresh_seconds": 30, "docker_refresh_seconds": 8, "tunnel_refresh_seconds": 15},
    "responsive": {"dashboard_refresh_seconds": 2, "background_refresh_seconds": 30, "kubernetes_refresh_seconds": 15, "docker_refresh_seconds": 5, "tunnel_refresh_seconds": 10},
}
PERFORMANCE_LIMITS = {
    "dashboard_refresh_seconds": (1, 60),
    "background_refresh_seconds": (10, 600),
    "kubernetes_refresh_seconds": (5, 600),
    "docker_refresh_seconds": (3, 300),
    "tunnel_refresh_seconds": (5, 300),
}
DEFAULT_PERFORMANCE_SETTINGS = {"profile": "balanced", **PERFORMANCE_PROFILES["balanced"], "pause_dashboard_when_inactive": False}


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(minimum, min(maximum, value))


def normalize_performance_settings(all_settings=None):
    all_settings = all_settings if isinstance(all_settings, dict) else {}
    raw = all_settings.get("performance_settings", {})
    raw = raw if isinstance(raw, dict) else {}
    profile = str(raw.get("profile", "balanced") or "balanced")
    if profile not in (*PERFORMANCE_PROFILES.keys(), "custom"):
        profile = "balanced"
    baseline = PERFORMANCE_PROFILES.get(profile, PERFORMANCE_PROFILES["balanced"])
    result = {"profile": profile}
    for key, (minimum, maximum) in PERFORMANCE_LIMITS.items():
        default = baseline.get(key, DEFAULT_PERFORMANCE_SETTINGS[key])
        value = raw.get(key, default) if profile == "custom" else default
        result[key] = _bounded_int(value, default, minimum, maximum)
    result["pause_dashboard_when_inactive"] = bool(raw.get("pause_dashboard_when_inactive", False))
    return result


def normalize_connection_preferences(all_settings=None):
    all_settings = all_settings if isinstance(all_settings, dict) else {}
    raw = all_settings.get("connection_preferences", {})
    raw = raw if isinstance(raw, dict) else {}
    return {
        "ssh_connect_timeout_seconds": _bounded_int(raw.get("ssh_connect_timeout_seconds", 10), 10, 3, 60),
        "ssh_keepalive_seconds": _bounded_int(raw.get("ssh_keepalive_seconds", 15), 15, 0, 120),
        "remember_k8s_context": bool(all_settings.get("remember_k8s_context", True)),
    }


def k8s_connection_profile_key(host, port=22, user=""):
    """Return a stable opaque key; raw hostnames and usernames aren't stored as map keys."""
    identity = json.dumps([str(host or "").strip().lower(), str(port or 22).strip(), str(user or "").strip().lower()], separators=(",", ":"))
    if not host and not user:
        return ""
    return hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()
