"""Smoke tests for the embedded remote-control interface."""
from webapp.static_content import INDEX_HTML


def test_remote_management_navigation_is_embedded():
    for marker in (
        'data-remote-view="overview"',
        'data-remote-view="terminal"',
        'data-remote-view="files"',
        'data-remote-view="docker"',
        'data-remote-view="operations"',
        'remote-context-apply',
        'remote-upload',
        'remote-operations-refresh',
    ):
        assert marker in INDEX_HTML


def test_remote_ui_uses_authenticated_backend_routes():
    for route in (
        "/api/remote/target",
        "/api/kube/contexts",
        "/api/remote/terminal",
        "/api/remote/files",
        "/api/remote/docker/",
        "/api/remote/docker/resources/remove",
        "/api/operations",
        "/api/app/status",
        "/api/app/settings",
        "/api/app/tunnels/start",
        "/api/app/machines",
        "/api/app/machines/connect",
        "/api/kube/resources/apply",
        "/api/kube/resources/validate",
        "/api/kube/contexts/inspect",
        "/api/kube/contexts/register",
        "/api/kube/contexts/delete",
        "/api/remote/system/process/terminate",
        "/api/remote/system/service",
        "/api/remote/system/power",
        "/api/remote/system/identity",
    ):
        assert route in INDEX_HTML
