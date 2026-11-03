"""Tests for shared command worker ownership and duplicate suppression."""

from services.command import CommandService


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self):
        for callback in tuple(self.callbacks):
            callback()


class FakeWorker:
    def __init__(self, running=True):
        self.finished = Signal()
        self.started = Signal()
        self.result = Signal()
        self.error = Signal()
        self.running = running
        self.stop_requested = False

    def isRunning(self):
        return self.running

    def cancel(self):
        self.stop_requested = True

    def finish(self):
        self.running = False
        self.finished.emit()


class FakeConnection:
    pass


def test_register_worker_deduplicates_active_operation_key():
    service = CommandService(FakeConnection())
    first = FakeWorker()
    duplicate = FakeWorker()

    assert service.register_worker(first, operation_key="dashboard:refresh") is first
    assert service.register_worker(duplicate, operation_key="dashboard:refresh") is first
    assert service.active_worker_count() == 1
    assert service.active_operation_count() == 1


def test_operation_key_can_be_reused_after_worker_finishes():
    service = CommandService(FakeConnection())
    first = FakeWorker()
    second = FakeWorker()

    service.register_worker(first, operation_key="docker:containers")
    first.finish()
    assert service.active_operation_count() == 0

    assert service.register_worker(second, operation_key="docker:containers") is second
    assert service.active_operation_count() == 1


def test_shutdown_cancels_registered_workers():
    service = CommandService(FakeConnection())
    worker = FakeWorker()
    service.register_worker(worker)

    service.request_worker_shutdown()

    assert worker.stop_requested is True



def test_worker_result_updates_service_diagnostics():
    from services.lifecycle import ServiceLifecycleManager

    manager = ServiceLifecycleManager()
    manager.register("monitoring", object())
    service = CommandService(FakeConnection())
    service._lifecycle_manager = manager
    worker = FakeWorker()

    service.register_worker(worker, diagnostic_key="dashboard:host-metrics")
    worker.started.emit()
    worker.result.emit("metrics", "", 0)

    status = manager.status("monitoring")
    assert status.last_success_at is not None
    assert status.last_refresh_at is not None
    assert status.last_duration_seconds is not None
    assert status.last_error is None
    assert service.worker_counts_by_service() == {"monitoring": 1}


def test_worker_error_updates_service_diagnostics():
    from services.lifecycle import ServiceLifecycleManager

    manager = ServiceLifecycleManager()
    manager.register("docker", object())
    service = CommandService(FakeConnection())
    service._lifecycle_manager = manager
    worker = FakeWorker()

    service.register_worker(worker, diagnostic_key="docker:list:containers")
    worker.started.emit()
    worker.error.emit("docker daemon unavailable")

    status = manager.status("docker")
    assert status.last_error == "docker daemon unavailable"
    assert status.last_duration_seconds is not None
    assert service.worker_counts_by_service() == {"docker": 1}
