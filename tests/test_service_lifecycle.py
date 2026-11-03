"""Tests for the in-process service lifecycle manager."""
import pytest

from services.lifecycle import ServiceLifecycleManager, ServiceState


class RecordingService:
    def __init__(self, *, fail_start=False):
        self.calls = []
        self.fail_start = fail_start

    def initialize(self):
        self.calls.append("initialize")

    def start(self):
        self.calls.append("start")
        if self.fail_start:
            raise RuntimeError("startup failed")

    def stop(self):
        self.calls.append("stop")

    def cleanup(self):
        self.calls.append("cleanup")


def test_registration_is_lazy_and_start_is_idempotent():
    manager = ServiceLifecycleManager()
    service = RecordingService()
    manager.register("feature", service)

    assert service.calls == []
    assert manager.status("feature").state == ServiceState.STOPPED

    manager.start("feature")
    manager.start("feature")
    assert service.calls == ["initialize", "start"]
    assert manager.status("feature").state == ServiceState.RUNNING


def test_dependency_starts_before_dependent_and_stops_in_reverse_order():
    manager = ServiceLifecycleManager()
    dependency = RecordingService()
    feature = RecordingService()
    manager.register("connection", dependency)
    manager.register("feature", feature, dependencies=("connection",))

    manager.start("feature")
    assert dependency.calls == ["initialize", "start"]
    assert feature.calls == ["initialize", "start"]

    manager.stop("feature")
    assert feature.calls[-2:] == ["stop", "cleanup"]


def test_start_failure_is_isolated_and_reported():
    manager = ServiceLifecycleManager()
    broken = RecordingService(fail_start=True)
    healthy = RecordingService()
    manager.register("broken", broken)
    manager.register("healthy", healthy)

    with pytest.raises(RuntimeError, match="startup failed"):
        manager.start("broken")
    assert manager.status("broken").state == ServiceState.FAILED
    assert "startup failed" in manager.status("broken").last_error

    manager.start("healthy")
    assert manager.status("healthy").state == ServiceState.RUNNING


def test_operation_diagnostics_are_credential_free_and_updated():
    manager = ServiceLifecycleManager()
    manager.register("monitoring", object())
    status = manager.record_operation(
        "monitoring", duration_seconds=0.25, success=False,
        error="metrics request failed", refresh=True,
    )
    assert status.last_duration_seconds == 0.25
    assert status.last_error == "metrics request failed"
    assert status.last_refresh_at is not None


def test_shutdown_is_idempotent():
    manager = ServiceLifecycleManager()
    service = RecordingService()
    manager.register("feature", service)
    manager.start("feature")
    manager.shutdown()
    manager.shutdown()
    assert service.calls.count("stop") == 1
    assert manager.status("feature").state == ServiceState.STOPPED


class PausableService:
    def __init__(self):
        self.paused = False

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


def test_pause_and_resume_preserve_running_lifecycle_state():
    manager = ServiceLifecycleManager()
    service = PausableService()
    manager.register("monitoring", service)
    manager.start("monitoring")

    paused = manager.pause("monitoring")
    assert paused.state == ServiceState.RUNNING
    assert paused.paused is True

    resumed = manager.resume("monitoring")
    assert resumed.state == ServiceState.RUNNING
    assert resumed.paused is False



def test_bound_service_becomes_running_on_first_use():
    from services.lifecycle import activate_lifecycle

    manager = ServiceLifecycleManager()
    service = object()
    manager.register("feature", service)
    service._lifecycle_manager = manager
    service._lifecycle_name = "feature"

    activate_lifecycle(service)

    assert manager.status("feature").state == ServiceState.RUNNING
