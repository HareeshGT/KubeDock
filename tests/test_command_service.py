"""Tests for shared command worker ownership and duplicate suppression."""

from services.command import CommandService


class Signal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in tuple(self.callbacks):
            callback(*args)


class FakeWorker:
    def __init__(self, running=True):
        self.finished = Signal()
        self.started = Signal()
        self.result = Signal()
        self.error = Signal()
        self.running = running
        self.finished_state = False
        self.stop_requested = False

    def isRunning(self):
        return self.running

    def isFinished(self):
        return self.finished_state

    def deleteLater(self):
        self.deleted = True

    def cancel(self):
        self.stop_requested = True

    def finish(self):
        self.running = False
        self.finished_state = True
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



def test_unstarted_worker_reserves_operation_key():
    service = CommandService(FakeConnection())
    first = FakeWorker(running=False)
    duplicate = FakeWorker(running=False)

    assert service.register_worker(first, operation_key="docker:list:containers") is first
    assert service.register_worker(duplicate, operation_key="docker:list:containers") is first
    assert service.registered_worker_count() == 1



def test_feature_stop_cancels_only_workers_owned_by_that_service():
    service = CommandService(FakeConnection())
    docker_worker = FakeWorker()
    dashboard_worker = FakeWorker()

    service.register_worker(docker_worker, diagnostic_key="docker:list:containers")
    service.register_worker(dashboard_worker, diagnostic_key="dashboard:host-metrics")

    assert service.stop_service("docker") == 1
    assert docker_worker.stop_requested is True
    assert dashboard_worker.stop_requested is False



def test_command_worker_cancel_closes_active_channel():
    from workers import CommandWorker

    class Channel:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    worker = CommandWorker(None, "sleep 30")
    channel = Channel()
    worker._set_active_channel(channel)

    worker.cancel()

    assert worker._cancel_event.is_set()
    assert channel.closed is True


def test_command_worker_cancel_before_channel_open_closes_later_channel():
    from workers import CommandWorker

    class Channel:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    worker = CommandWorker(None, "sleep 30")
    worker.cancel()
    channel = Channel()

    worker._set_active_channel(channel)

    assert channel.closed is True
