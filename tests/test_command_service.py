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
