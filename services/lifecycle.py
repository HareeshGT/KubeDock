"""Lightweight, in-process lifecycle coordination for feature services.

The manager owns lifecycle state, not feature implementation. Services remain
ordinary Python objects and only start optional work when explicitly requested.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import threading
import time
from typing import Any, Callable, Dict, Optional


class ServiceState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    state: ServiceState
    paused: bool = False
    active_operations: int = 0
    worker_count: int = 0
    last_success_at: Optional[float] = None
    last_refresh_at: Optional[float] = None
    last_error: Optional[str] = None
    last_duration_seconds: Optional[float] = None
    updated_at: float = field(default_factory=time.time)


@dataclass
class _Entry:
    service: Any
    dependencies: tuple[str, ...] = ()
    state: ServiceState = ServiceState.STOPPED
    paused: bool = False
    active_operations: int = 0
    worker_count: int = 0
    last_success_at: Optional[float] = None
    last_refresh_at: Optional[float] = None
    last_error: Optional[str] = None
    last_duration_seconds: Optional[float] = None


class ServiceLifecycleManager:
    """Thread-safe lifecycle registry with dependency-aware startup/shutdown.

    No service is started during registration. This is deliberate: optional
    feature work must remain lazy and should only begin when a feature is used.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._entries: Dict[str, _Entry] = {}
        self._closed = False

    def register(self, name: str, service: Any, dependencies=()) -> None:
        key = str(name).strip().lower()
        if not key:
            raise ValueError("Service name cannot be empty")
        with self._lock:
            if self._closed:
                raise RuntimeError("Lifecycle manager is shut down")
            if key in self._entries:
                raise ValueError(f"Service already registered: {key}")
            self._entries[key] = _Entry(service, tuple(dependencies or ()))

    def _entry(self, name: str) -> _Entry:
        try:
            return self._entries[str(name).lower()]
        except KeyError:
            raise KeyError(f"Unknown service: {name}") from None

    def start(self, name: str) -> ServiceStatus:
        key = str(name).lower()
        with self._lock:
            if self._closed:
                raise RuntimeError("Lifecycle manager is shut down")
            entry = self._entry(key)
            if entry.state == ServiceState.RUNNING:
                return self.status(key)
            if entry.state in (ServiceState.STARTING, ServiceState.STOPPING):
                raise RuntimeError(f"Service {key} is transitioning ({entry.state.value})")
            dependencies = entry.dependencies
            entry.state = ServiceState.STARTING
            entry.paused = False
            entry.last_error = None
        try:
            for dependency in dependencies:
                dep_status = self.start(dependency)
                if dep_status.state != ServiceState.RUNNING:
                    raise RuntimeError(f"Dependency {dependency} is not running")
            initialize = getattr(entry.service, "initialize", None)
            if callable(initialize):
                initialize()
            start = getattr(entry.service, "start", None)
            if callable(start):
                start()
            with self._lock:
                entry.state = ServiceState.RUNNING
                entry.last_success_at = time.time()
                return self.status(key)
        except Exception as exc:
            with self._lock:
                entry.state = ServiceState.FAILED
                entry.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            raise

    def stop(self, name: str) -> ServiceStatus:
        key = str(name).lower()
        with self._lock:
            entry = self._entry(key)
            if entry.state == ServiceState.STOPPED:
                return self.status(key)
            if entry.state == ServiceState.STOPPING:
                return self.status(key)
            entry.state = ServiceState.STOPPING
        try:
            stop = getattr(entry.service, "stop", None)
            if callable(stop):
                stop()
            cleanup = getattr(entry.service, "cleanup", None)
            if callable(cleanup):
                cleanup()
            with self._lock:
                entry.state = ServiceState.STOPPED
                entry.paused = False
                return self.status(key)
        except Exception as exc:
            with self._lock:
                entry.state = ServiceState.FAILED
                entry.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            raise

    def pause(self, name: str) -> ServiceStatus:
        with self._lock:
            entry = self._entry(name)
            if entry.state != ServiceState.RUNNING:
                raise RuntimeError(f"Service {name} must be running before it can be paused")
            if entry.paused:
                return self.status(name)
        pause = getattr(entry.service, "pause", None)
        if not callable(pause):
            raise NotImplementedError(f"Service {name} does not support pause")
        pause()
        with self._lock:
            entry.paused = True
            return self.status(name)

    def resume(self, name: str) -> ServiceStatus:
        with self._lock:
            entry = self._entry(name)
            if entry.state == ServiceState.RUNNING and not entry.paused:
                return self.status(name)
            if entry.state != ServiceState.RUNNING:
                return self.start(name)
        resume = getattr(entry.service, "resume", None)
        if not callable(resume):
            raise NotImplementedError(f"Service {name} does not support resume")
        resume()
        with self._lock:
            entry.paused = False
            entry.last_success_at = time.time()
            return self.status(name)

    def restart(self, name: str) -> ServiceStatus:
        self.stop(name)
        return self.start(name)

    def record_operation(self, name: str, *, duration_seconds=None,
                         success=True, error=None, refresh=False) -> ServiceStatus:
        with self._lock:
            entry = self._entry(name)
            if duration_seconds is not None:
                entry.last_duration_seconds = max(0.0, float(duration_seconds))
            if success:
                entry.last_success_at = time.time()
                entry.last_error = None
            else:
                entry.last_error = str(error or "Operation failed")[:1000]
            if refresh:
                entry.last_refresh_at = time.time()
            return self.status(name)

    def set_activity(self, name: str, *, active_operations=None,
                     worker_count=None) -> ServiceStatus:
        with self._lock:
            entry = self._entry(name)
            if active_operations is not None:
                entry.active_operations = max(0, int(active_operations))
            if worker_count is not None:
                entry.worker_count = max(0, int(worker_count))
            return self.status(name)

    def status(self, name: str) -> ServiceStatus:
        with self._lock:
            e = self._entry(name)
            return ServiceStatus(
                name=str(name).lower(), state=e.state, paused=e.paused,
                active_operations=e.active_operations,
                worker_count=e.worker_count,
                last_success_at=e.last_success_at,
                last_refresh_at=e.last_refresh_at,
                last_error=e.last_error,
                last_duration_seconds=e.last_duration_seconds,
            )

    def statuses(self) -> Dict[str, ServiceStatus]:
        with self._lock:
            names = tuple(self._entries)
        return {name: self.status(name) for name in names}

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            names = tuple(reversed(tuple(self._entries)))
        errors = []
        for name in names:
            try:
                self.stop(name)
            except Exception as exc:
                errors.append((name, exc))
        if errors:
            raise RuntimeError("One or more services failed to stop: " +
                               ", ".join(name for name, _ in errors))
