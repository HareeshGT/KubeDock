"""Central connection/session lifecycle for KubeDock.

The service is deliberately UI- and Paramiko-policy agnostic: connection
workers still establish sessions, while this service becomes the single
owner of the active session, its metadata, state transitions and teardown.
Existing feature modules can temporarily obtain the underlying client through
``get_ssh_client`` while they are migrated to narrower service interfaces.
"""

from __future__ import annotations

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

    @property
    def snapshot(self) -> ConnectionSnapshot:
        with self._lock:
            return self._snapshot

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
            generation = self._snapshot.generation + 1
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

        # Do not close an object deliberately reused as the new resource.
        # Teardown runs outside the state lock.
        if old_ssh is not None and old_ssh is not ssh:
            self._pool_closer(old_ssh)
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
        return self._replace_session(
            protocol="ssh",
            ssh=ssh,
            filesystem=filesystem,
            host=host,
            port=port,
            username=username,
            alias=alias,
        )

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

    def get_ssh_client(self):
        """Return raw SSH for legacy adapters during incremental migration.

        New services should prefer operation-specific methods and should not
        expose this object to UI or HTTP request handlers.
        """
        with self._lock:
            if not self._snapshot.is_active:
                return None
            return self._ssh

    def get_filesystem(self):
        """Return the active app-facing filesystem adapter, if any."""
        with self._lock:
            if not self._snapshot.is_active:
                return None
            return self._filesystem

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
            self._ssh = None
            self._filesystem = None
            self._snapshot = snapshot
            listeners = tuple(self._listeners.values())

        # Publish first so consumers stop scheduling work against resources
        # that are being closed.
        self._notify(listeners, snapshot)
        if ssh is not None:
            self._pool_closer(ssh)
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
