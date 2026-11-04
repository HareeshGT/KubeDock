"""Composition-root integration tests for the in-process service graph."""

from services.runtime import AppServices
from services.lifecycle import ServiceState


def test_app_services_share_one_connection_and_command_runtime():
    runtime = AppServices()

    assert runtime.kubernetes.connection is runtime.connection
    assert runtime.docker.connection is runtime.connection
    assert runtime.dashboard.connection is runtime.connection
    assert runtime.files.connection is runtime.connection
    assert runtime.terminal.commands is runtime.commands
    assert runtime.ai.commands is runtime.commands
    assert runtime.design_intelligence.status()["service"] == "design-intelligence-system"
    assert runtime.design_intelligence.enabled is False
    assert runtime.webapp.connection is runtime.connection
    assert runtime.settings.database is runtime.database
    assert runtime.lifecycle.status("database").state == ServiceState.STOPPED
    assert runtime.lifecycle.status("settings").state == ServiceState.STOPPED

    assert runtime.commands.registered_worker_count() == 0
    assert runtime.commands.active_worker_count() == 0
    runtime.shutdown()


def test_first_feature_use_activates_dependencies_and_shutdown_stops_them():
    runtime = AppServices()

    runtime.lifecycle.start("docker")
    statuses = runtime.lifecycle.statuses()
    assert statuses["connection"].state == ServiceState.RUNNING
    assert statuses["commands"].state == ServiceState.RUNNING
    assert statuses["docker"].state == ServiceState.RUNNING

    runtime.shutdown()
    statuses = runtime.lifecycle.statuses()
    assert all(status.state == ServiceState.STOPPED for status in statuses.values())
