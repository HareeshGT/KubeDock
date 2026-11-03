"""Central connection/session lifecycle for KubeDock.

The service is deliberately UI- and Paramiko-policy agnostic: connection
workers still establish sessions, while this service becomes the single
owner of the active session, its metadata, state transitions and teardown.
Existing feature modules can temporarily obtain the underlying client through
``get_ssh_client`` while they are migrated to narrower service interfaces.
"""

from __future__ import annotations

from .lifecycle import activate_lifecycle

from dataclasses import dataclass
import logging
import threading
import time
from typing import Callable, Optional
from uuid import uuid4

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConnectionSnapshot:
    """Safe, credential-free view of the active connection."""

    state: str = "disconnected"
    protocol: str = ""
    host: str = ""
    port: Optional[int] = None
    username: str = ""
    alias: str = ""
    session_id: Optional[str] = None
    generation: int = 0
    connected_at: Optional[float] = None
    latency_ms: Optional[float] = None
    last_error: Optional[str] = None

    @property
    def is_active(self) -> bool:
        return self.state in {"connected", "degraded"}


class ConnectionService:
    """Own the active SSH or FTP(S) session used by the application.

    This is an in-process service, not a separate server or process. It does
    not establish connections itself; connection workers hand completed
    sessions to it. SSH's existing bounded secondary-channel pool remains in
    place and is closed before the primary transport during teardown.

    No passwords or private-key contents/paths are retained in the service's
    snapshot or event payloads.
    """

    def __init__(self, pool_closer: Optional[Callable[[object], None]] = None):
        self._lock = threading.RLock()
        self._ssh = None
        self._filesystem = None
        self._snapshot = ConnectionSnapshot()
        self._listeners = {}
        self._next_listener = 1
        self._bound_workers = {}
        self._pool_closer = pool_closer or self._close_ssh_pool

    @staticmethod
    def _close_ssh_pool(ssh) -> None:
        if ssh is None:
            return
        try:
            # Lazy import avoids a services -> workers import at application
            # startup and keeps the service independently unit-testable.
            from workers import close_ssh_connection_pool

            close_ssh_connection_pool(ssh)
        except Exception:
            _LOG.exception("Unable to close the managed SSH connection pool")

    def _close_pool_quietly(self, ssh) -> None:
        try:
            self._pool_closer(ssh)
        except Exception:
            # A pool teardown problem must not prevent primary resources
            # from being closed.
            _LOG.exception("Managed SSH pool teardown failed")

    @property
    def snapshot(self) -> ConnectionSnapshot:
        with self._lock:
            return self._snapshot

    def bind_worker(self, worker, snapshot: Optional[ConnectionSnapshot] = None):
        """Bind an asynchronous worker to one immutable session generation.

        Workers are also invalidated immediately when that session is replaced
        or disconnected, so a blocking socket read cannot outlive a host switch.
        """
        captured = snapshot or self.snapshot
        worker.connection_generation = captured.generation
        worker.connection_session_id = captured.session_id
        worker._session_guard = (
            lambda generation=captured.generation: self.is_current(generation)
        )
        with self._lock:
            current = (
                self._snapshot.is_active
                and self._snapshot.generation == captured.generation
            )
            if current:
                self._bound_workers.setdefault(captured.generation, []).append(worker)
        try:
            worker.finished.connect(
                lambda w=worker, g=captured.generation: self._forget_bound_worker(g, w)
            )
        except Exception:
            pass
        if not current:
            self._request_worker_stop(worker)
        return worker

    def unbind_worker(self, worker):
        """Remove a worker that lost an operation-key registration race."""
        generation = getattr(worker, "connection_generation", None)
        if generation is not None:
            self._forget_bound_worker(generation, worker)

    def _forget_bound_worker(self, generation, worker):
        with self._lock:
            workers = self._bound_workers.get(generation, [])
            if worker in workers:
                workers.remove(worker)
            if not workers:
                self._bound_workers.pop(generation, None)

    @staticmethod
    def _request_worker_stop(worker):
        for method_name in ("cancel", "request_stop", "stop"):
            method = getattr(worker, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    _LOG.debug("Unable to stop stale connection worker", exc_info=True)
                return
        try:
            worker.requestInterruption()
        except Exception:
            pass

    def _take_bound_workers(self, generation):
        with self._lock:
            return self._bound_workers.pop(generation, [])

    def subscribe(self, listener: Callable[[ConnectionSnapshot], None]) -> int:
        """Subscribe to state changes; returns a token for unsubscribe."""
        if not callable(listener):
            raise TypeError("listener must be callable")
        with self._lock:
            token = self._next_listener
            self._next_listener += 1
            self._listeners[token] = listener
            current = self._snapshot
        # Deliver current state immediately to avoid a subscribe/query race.
        self._notify((listener,), current)
        return token

    def unsubscribe(self, token: int) -> None:
        with self._lock:
            self._listeners.pop(token, None)

    def _notify(self, listeners, snapshot: ConnectionSnapshot) -> None:
        for listener in listeners:
            try:
                listener(snapshot)
            except Exception:
                _LOG.exception("Connection state listener failed")

    def _publish(self, snapshot: ConnectionSnapshot) -> None:
        with self._lock:
            listeners = tuple(self._listeners.values())
        self._notify(listeners, snapshot)

    def _replace_session(
        self,
        *,
        protocol: str,
        ssh,
        filesystem,
        host: str,
        port: Optional[int],
        username: str,
        alias: str,
    ) -> ConnectionSnapshot:
        if protocol == "ssh" and ssh is None:
            raise ValueError("An SSH session requires an SSH client")
        if protocol in {"ftp", "ftps"} and filesystem is None:
            raise ValueError("A file-transfer session requires a filesystem client")

        with self._lock:
            old_ssh, old_filesystem = self._ssh, self._filesystem
            old_generation = self._snapshot.generation
            generation = old_generation + 1
            snapshot = ConnectionSnapshot(
                state="connected",
                protocol=protocol,
                host=str(host or ""),
                port=int(port) if port is not None else None,
                username=str(username or ""),
                alias=str(alias or ""),
                session_id=uuid4().hex,
                generation=generation,
                connected_at=time.time(),
            )
            self._ssh = ssh
            self._filesystem = filesystem
            self._snapshot = snapshot
            listeners = tuple(self._listeners.values())

        # Stop workers bound to the old session before tearing down its resources.
        stale_workers = self._take_bound_workers(old_generation)
        for worker in stale_workers:
            self._request_worker_stop(worker)

        # Do not close an object deliberately reused as the new resource.
        # Teardown runs outside the state lock.
        if old_ssh is not None and old_ssh is not ssh:
            self._close_pool_quietly(old_ssh)
            if old_filesystem is not filesystem:
                self._close_quietly(old_filesystem)
            self._close_quietly(old_ssh)
        elif old_filesystem is not None and old_filesystem is not filesystem:
            self._close_quietly(old_filesystem)

        self._notify(listeners, snapshot)
        return snapshot

    def attach_ssh(
        self,
        ssh,
        filesystem=None,
        *,
        host: str = "",
        port: int = 22,
        username: str = "",
        alias: str = "",
    ) -> ConnectionSnapshot:
        """Adopt a connected SSH client and its app-facing filesystem."""
        activate_lifecycle(self)
        return self._replace_session(
            protocol="ssh",
            ssh=ssh,
            filesystem=filesystem,
            host=host,
            port=port,
            username=username,
            alias=alias,
        )

    @staticmethod
    def create_ssh_connect_worker(host, port, username, key_path="", password=""):
        """Create the existing background SSH handshake worker."""
        from workers import ConnectWorker

        return ConnectWorker(host, port, username, key_path or "", password or "")

    @staticmethod
    def create_file_connect_worker(host, port, username, password, *, tls=False, passive=True):
        """Create the existing background FTP/FTPS handshake worker."""
        from workers import FTPConnectionWorker

        return FTPConnectionWorker(
            host, port, username, password, tls=bool(tls), passive=bool(passive),
        )

    def create_health_worker(self):
        """Create the single heartbeat worker bound to the active SSH generation."""
        ssh, snapshot = self.get_ssh_session()
        if ssh is None:
            raise RuntimeError("An active SSH session is required for connection health monitoring")
        from workers import ConnectionHealthWorker

        worker = ConnectionHealthWorker(ssh)
        self.bind_worker(worker, snapshot)
        return worker

    def attach_file_connection(
        self,
        filesystem,
        *,
        protocol: str,
        host: str = "",
        port: Optional[int] = None,
        username: str = "",
        alias: str = "",
    ) -> ConnectionSnapshot:
        """Adopt an FTP or FTPS filesystem session (without SSH access)."""
        activate_lifecycle(self)
        normalized = str(protocol or "").lower()
        if normalized not in {"ftp", "ftps"}:
            raise ValueError("protocol must be 'ftp' or 'ftps'")
        return self._replace_session(
            protocol=normalized,
            ssh=None,
            filesystem=filesystem,
            host=host,
            port=port,
            username=username,
            alias=alias,
        )

    def get_ssh_session(self):
        """Atomically return the active SSH client and its immutable snapshot."""
        with self._lock:
            if not self._snapshot.is_active:
                return None, self._snapshot
            return self._ssh, self._snapshot

    def get_ssh_client(self):
        """Return raw SSH for legacy adapters during incremental migration.

        New services should prefer operation-specific methods and should not
        expose this object to UI or HTTP request handlers.
        """
        return self.get_ssh_session()[0]

    def get_filesystem_session(self):
        """Atomically return the filesystem adapter and its snapshot."""
        with self._lock:
            if not self._snapshot.is_active:
                return None, self._snapshot
            return self._filesystem, self._snapshot

    def get_file_session(self):
        """Atomically return filesystem, SSH client, and the owning snapshot."""
        with self._lock:
            if not self._snapshot.is_active:
                return None, None, self._snapshot
            return self._filesystem, self._ssh, self._snapshot

    def get_filesystem(self):
        """Return the active app-facing filesystem adapter, if any."""
        return self.get_filesystem_session()[0]

    def is_current(self, generation: int) -> bool:
        """Check whether an asynchronous result belongs to the live session."""
        with self._lock:
            return (
                self._snapshot.is_active
                and self._snapshot.generation == generation
            )

    def mark_degraded(self, detail: str = "") -> ConnectionSnapshot:
        return self._set_health("degraded", detail=detail)

    def mark_recovered(self, latency_ms: Optional[float] = None) -> ConnectionSnapshot:
        with self._lock:
            current = self._snapshot
            if not current.is_active:
                return current
            snapshot = ConnectionSnapshot(
                **{
                    **current.__dict__,
                    "state": "connected",
                    "latency_ms": max(0.0, float(latency_ms)) if latency_ms is not None else current.latency_ms,
                    "last_error": None,
                }
            )
            self._snapshot = snapshot
        self._publish(snapshot)
        return snapshot

    def mark_lost(self, detail: str = "") -> ConnectionSnapshot:
        return self._set_health("lost", detail=detail)

    def _set_health(self, state: str, detail: str = "") -> ConnectionSnapshot:
        with self._lock:
            current = self._snapshot
            if not current.is_active:
                return current
            snapshot = ConnectionSnapshot(
                **{
                    **current.__dict__,
                    "state": state,
                    "last_error": str(detail or "")[:1000] or None,
                }
            )
            self._snapshot = snapshot
            stale_workers = (
                self._bound_workers.pop(current.generation, [])
                if state == "lost" else []
            )
        for worker in stale_workers:
            self._request_worker_stop(worker)
        self._publish(snapshot)
        return snapshot

    def disconnect(self) -> ConnectionSnapshot:
        """Clear the active session and close its resources exactly once."""
        with self._lock:
            ssh, filesystem = self._ssh, self._filesystem
            current = self._snapshot
            if ssh is None and filesystem is None and current.state == "disconnected":
                return current
            snapshot = ConnectionSnapshot(
                state="disconnected",
                generation=current.generation + 1,
            )
            stale_workers = self._bound_workers.pop(current.generation, [])
            self._ssh = None
            self._filesystem = None
            self._snapshot = snapshot
            listeners = tuple(self._listeners.values())

        # Publish first so consumers stop scheduling work against resources
        # that are being closed, and stop bound workers before socket teardown.
        self._notify(listeners, snapshot)
        for worker in stale_workers:
            self._request_worker_stop(worker)
        if ssh is not None:
            self._close_pool_quietly(ssh)
        self._close_quietly(filesystem)
        if ssh is not None:
            self._close_quietly(ssh)
        return snapshot

    @staticmethod
    def _close_quietly(resource) -> None:
        if resource is None:
            return
        try:
            resource.close()
        except Exception:
            _LOG.debug("Connection resource close failed", exc_info=True)
