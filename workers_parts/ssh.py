"""KubeDock workers split module.

This module is an internal implementation module. Public compatibility
symbols are re-exported by the top-level ``workers.py`` facade.
"""

import codecs
import os
import re
import signal
import mimetypes
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from PyQt5.QtCore import QThread, pyqtSignal

from contextlib import contextmanager

_SSH_POOL_MAX_CONNECTIONS = 5  # Includes the primary SSH transport.
_SSH_POOL_MAX_SECONDARY_CONNECTIONS = _SSH_POOL_MAX_CONNECTIONS - 1
_SSH_SCALE_UP_UTILIZATION = 0.70
_SSH_SCALE_DOWN_IDLE_SECONDS = 60.0
_SSH_POOL_MAINTENANCE_INTERVAL = 5.0
_SSH_MAX_CHANNELS_PER_CONNECTION = 6
_SSH_GUARD_ATTR = "_kdb_channel_guard"
_SSH_POOL_ATTR = "_kdb_connection_pool"
_DEFAULT_SESSION_WAIT = 30
# paramiko's Transport.open_session() waits up to 3600s (1h) for the server's
# reply when no timeout is given. On a half-open connection (NAT/VPN drop,
# laptop sleep/wake) that turns into an hour-long silent hang, so every
# channel open is bounded explicitly.
_CHANNEL_OPEN_TIMEOUT = 10
# After a failed secondary connect, don't let every waiting caller redo the
# (up to 30s) handshake one after another against a host that is down.
_CONNECT_RETRY_COOLDOWN = 3.0
_CONNECT_TIMEOUT = 10

def _configure_host_key_policy(ssh):
    """Require SSH host keys to be known before connecting."""
    import paramiko
    ssh.load_system_host_keys()
    known_hosts = os.path.expanduser("~/.ssh/known_hosts")
    if os.path.isfile(known_hosts):
        ssh.load_host_keys(known_hosts)
    ssh.set_missing_host_key_policy(paramiko.RejectPolicy())


def _configure_transport(ssh):
    """Apply the same transport tuning used by the primary connection."""
    try:
        transport = ssh.get_transport()
        if transport is None:
            return
        transport.default_window_size = 64 * 1024 * 1024
        try:
            transport.packetizer.REKEY_BYTES = pow(2, 40)
            transport.packetizer.REKEY_PACKETS = pow(2, 40)
        except Exception:
            pass
        transport.set_keepalive(15)
    except Exception:
        pass


class _SSHPoolSlot:
    """One secondary SSH connection and its session-channel limiter."""

    def __init__(self, ssh):
        self.ssh = ssh
        self.sem = threading.BoundedSemaphore(_SSH_MAX_CHANNELS_PER_CONNECTION)
        self.active_channels = 0
        self.last_used_at = time.monotonic()


class SSHConnectionPool:
    """Lazily scales secondary SSH transports around one primary connection.

    The configured maximum counts the primary transport, so the default of
    five means one primary plus at most four secondary transports. Secondary
    transports multiplex up to six channels each. New transports are created
    when every existing transport reaches the utilization threshold; idle
    secondary transports are reaped after the configured grace period.

    Robustness guarantees:
      * a channel open is always bounded (``_CHANNEL_OPEN_TIMEOUT``); a
        connection that stops answering is evicted and the acquire is
        retried once on a fresh connection, transparently to the caller;
      * a caller's ``timeout`` is honoured in every branch (waiting, and
        connecting), not only while waiting for a free slot;
      * after a failed connect, callers fail fast for a short cooldown
        instead of each repeating the handshake against a dead host;
      * blocking ``close()`` calls on paramiko clients never run while the
        pool lock is held, so one slow teardown can't stall every caller.
    """

    def __init__(self, host, port, user, pem="", password="", max_connections=None,
                 max_total_connections=_SSH_POOL_MAX_CONNECTIONS,
                 scale_up_utilization=_SSH_SCALE_UP_UTILIZATION,
                 idle_timeout_seconds=_SSH_SCALE_DOWN_IDLE_SECONDS,
                 maintenance_interval=_SSH_POOL_MAINTENANCE_INTERVAL):
        self.host = host
        self.port = port
        self.user = user
        self.pem = pem or ""
        self.password = password or ""
        # max_connections is retained as a compatibility alias for the old
        # secondary-pool limit. New callers should use max_total_connections,
        # which includes the primary SSH transport.
        if max_connections is not None:
            self.max_connections = max(0, int(max_connections))
        else:
            self.max_connections = max(0, int(max_total_connections) - 1)
        self.max_total_connections = self.max_connections + 1
        self.scale_up_utilization = min(1.0, max(0.1, float(scale_up_utilization)))
        self.idle_timeout_seconds = max(0.0, float(idle_timeout_seconds))
        self.maintenance_interval = max(0.1, float(maintenance_interval))
        self._slots = []
        self._selection_cursor = 0
        self._creating = False
        self._closed = False
        self._fail_until = 0.0
        self._fail_exc = None
        self._cond = threading.Condition(threading.RLock())
        self._maintenance_stop = threading.Event()
        self._maintenance_thread = threading.Thread(
            target=self._maintenance_loop, name="kdb-ssh-pool-maintenance", daemon=True,
        )
        self._maintenance_thread.start()

    # ── connection creation ──────────────────────────────────
    def _connect_secondary(self, budget=None):
        import paramiko

        t = _CONNECT_TIMEOUT if budget is None else max(1.0, min(_CONNECT_TIMEOUT, budget))
        ssh = paramiko.SSHClient()
        try:
            # Keep the same host-key behavior as the existing primary
            # connection so introducing the pool does not change semantics.
            _configure_host_key_policy(ssh)
            kw = dict(
                hostname=self.host,
                port=self.port,
                username=self.user,
                timeout=t,
                banner_timeout=t,
                auth_timeout=t,
            )
            if self.password:
                kw["password"] = self.password
                kw["look_for_keys"] = False
                kw["allow_agent"] = False
            elif self.pem:
                kw["key_filename"] = self.pem
            elif self.host in ["127.0.0.1", "localhost"]:
                pass
            ssh.connect(**kw)
            _configure_transport(ssh)
            return ssh
        except BaseException:
            # A failed connect()/auth leaves paramiko's transport thread and
            # socket behind unless the client is closed explicitly.
            try:
                ssh.close()
            except Exception:
                pass
            raise

    @staticmethod
    def _close_quietly(ssh):
        try:
            ssh.close()
        except Exception:
            pass

    # ── slot bookkeeping (call with self._cond held) ─────────
    def _find_slot_with_capacity(self, dead, below_scale_threshold=False):
        """Reserve a channel on the least-loaded healthy slot.

        Below the scale-up threshold we prefer existing capacity. Once every
        slot reaches the threshold, _acquire_slot creates another transport
        (until the configured cap); at the cap we use any remaining channel.
        """
        candidates = []
        threshold = max(1, int(_SSH_MAX_CHANNELS_PER_CONNECTION * self.scale_up_utilization + 0.999))
        for slot in list(self._slots):
            try:
                transport = slot.ssh.get_transport()
                active = transport is not None and transport.is_active()
            except Exception:
                active = False
            if not active:
                try:
                    self._slots.remove(slot)
                except ValueError:
                    pass
                dead.append(slot)
                continue
            if below_scale_threshold and slot.active_channels >= threshold:
                continue
            if slot.active_channels < _SSH_MAX_CHANNELS_PER_CONNECTION:
                candidates.append(slot)
        # Sort by in-flight channels, then use a stable rotating tie-breaker
        # so equally-loaded transports share work instead of always favouring
        # the first connection in the pool.
        candidates.sort(key=lambda item: (
            item.active_channels,
            (self._slots.index(item) - self._selection_cursor) % max(1, len(self._slots)),
        ))
        for slot in candidates:
            if slot.sem.acquire(False):
                slot.active_channels += 1
                slot.last_used_at = time.monotonic()
                self._selection_cursor = (self._slots.index(slot) + 1) % max(1, len(self._slots))
                return slot
        return None

    def _remove_slot(self, slot):
        with self._cond:
            try:
                self._slots.remove(slot)
            except ValueError:
                return
            self._cond.notify_all()
        self._close_quietly(slot.ssh)

    def _acquire_slot(self, deadline):
        """Reserve one channel on some pooled connection (creating one if
        needed) and return its slot. Raises on timeout/closed/connect error."""
        dead = []
        try:
            while True:
                with self._cond:
                    if self._closed:
                        raise RuntimeError("SSH connection pool is closed")

                    # First fill connections below the configured utilization
                    # threshold. If all are busy enough, scale out before
                    # exhausting their channel limits.
                    slot = self._find_slot_with_capacity(dead, below_scale_threshold=True)
                    if slot is not None:
                        return slot

                    now = time.monotonic()
                    remaining = None if deadline is None else deadline - now
                    cooling = self._fail_until > now and self._fail_exc is not None

                    if len(self._slots) < self.max_connections and not self._creating:
                        if cooling and not self._slots:
                            # Nothing to wait for and connecting just failed:
                            # fail fast rather than repeat the handshake.
                            raise RuntimeError(
                                "SSH secondary connection failed: {}".format(self._fail_exc)
                            )
                        if not cooling:
                            self._creating = True
                            create = True
                        else:
                            create = False
                    else:
                        create = False

                    # At the connection cap (or while another thread is
                    # creating the next transport), use the least-loaded
                    # remaining channel rather than waiting unnecessarily.
                    if not create and (len(self._slots) >= self.max_connections or self._creating or cooling):
                        slot = self._find_slot_with_capacity(dead, below_scale_threshold=False)
                        if slot is not None:
                            return slot

                    if not create:
                        if remaining is not None and remaining <= 0:
                            raise RuntimeError("SSH is busy; no pooled session channel is currently available")
                        # Wake at least once a second so the deadline is
                        # honoured and dead connections are noticed even if
                        # nobody calls notify().
                        wait_for = 1.0 if remaining is None else min(1.0, remaining)
                        self._cond.wait(wait_for)
                        continue

                # ── create a new secondary connection (lock released) ──
                budget = None if deadline is None else max(0.0, deadline - time.monotonic())
                new_slot = None
                try:
                    secondary = self._connect_secondary(budget=budget)
                    new_slot = _SSHPoolSlot(secondary)
                    new_slot.sem.acquire()  # reserve the first channel for this caller
                    new_slot.active_channels = 1
                except BaseException as exc:
                    with self._cond:
                        self._creating = False
                        self._fail_until = time.monotonic() + _CONNECT_RETRY_COOLDOWN
                        self._fail_exc = exc
                        self._cond.notify_all()
                    raise

                closed = False
                with self._cond:
                    self._creating = False
                    self._fail_until = 0.0
                    self._fail_exc = None
                    if self._closed:
                        closed = True
                    else:
                        self._slots.append(new_slot)
                    self._cond.notify_all()
                if closed:
                    self._close_quietly(new_slot.ssh)
                    raise RuntimeError("SSH connection pool is closed")
                return new_slot
        finally:
            for d in dead:
                self._close_quietly(d.ssh)

    # ── public API ───────────────────────────────────────────
    def acquire_channel(self, timeout=None):
        """Return ``(secondary_ssh, channel)`` and reserve one channel slot."""
        import paramiko

        deadline = None if timeout is None else time.monotonic() + timeout
        for attempt in range(2):
            slot = self._acquire_slot(deadline)
            try:
                transport = slot.ssh.get_transport()
                if transport is None or not transport.is_active():
                    raise paramiko.SSHException("Pooled SSH connection is no longer active")
                channel = transport.open_session(timeout=_CHANNEL_OPEN_TIMEOUT)
            except Exception as exc:
                self._release_slot(slot)
                # A server that *rejects* the channel (MaxSessions reached)
                # is healthy — keep the connection. Anything else (timeout,
                # EOF, socket error, dead transport) means this connection
                # is unusable: evict it so it can never be handed out again.
                unusable = not isinstance(exc, paramiko.ChannelException)
                if unusable:
                    self._remove_slot(slot)
                with self._cond:
                    self._cond.notify_all()
                if unusable and attempt == 0:
                    continue  # retry once on a fresh connection
                raise
            channel._kdb_pool_slot = slot
            channel._kdb_pool_owner = self
            return slot.ssh, channel
        raise RuntimeError("SSH pooled session could not be opened")  # pragma: no cover

    def _release_slot(self, slot):
        if slot is None:
            return
        with self._cond:
            if slot.active_channels > 0:
                slot.active_channels -= 1
            slot.last_used_at = time.monotonic()
            try:
                slot.sem.release()
            except ValueError:
                pass
            self._cond.notify_all()

    def release_channel(self, channel):
        slot = getattr(channel, "_kdb_pool_slot", None)
        if slot is None:
            return
        try:
            delattr(channel, "_kdb_pool_slot")
        except Exception:
            pass
        try:
            delattr(channel, "_kdb_pool_owner")
        except Exception:
            pass
        self._release_slot(slot)

    def _reap_idle_connections(self, now=None):
        """Close idle secondary transports; the primary transport is retained."""
        now = time.monotonic() if now is None else float(now)
        with self._cond:
            if self._closed:
                return 0
            idle = [slot for slot in self._slots
                    if slot.active_channels == 0
                    and now - slot.last_used_at >= self.idle_timeout_seconds]
            for slot in idle:
                self._slots.remove(slot)
            if idle:
                self._cond.notify_all()
        for slot in idle:
            self._close_quietly(slot.ssh)
        return len(idle)

    def _maintenance_loop(self):
        while not self._maintenance_stop.wait(self.maintenance_interval):
            try:
                self._reap_idle_connections()
            except Exception:
                # Maintenance is best-effort and must never kill command work.
                pass

    def configure(self, *, max_total_connections=None, scale_up_utilization=None,
                  idle_timeout_seconds=None):
        """Update safe runtime pool limits without retaining new credentials."""
        with self._cond:
            if max_total_connections is not None:
                total = max(1, int(max_total_connections))
                self.max_connections = total - 1
                self.max_total_connections = total
            if scale_up_utilization is not None:
                self.scale_up_utilization = min(1.0, max(0.1, float(scale_up_utilization)))
            if idle_timeout_seconds is not None:
                self.idle_timeout_seconds = max(0.0, float(idle_timeout_seconds))
            self._cond.notify_all()

    def snapshot(self):
        """Return credential-free pool capacity and utilization metrics."""
        with self._cond:
            return {
                "total_connections": 1 + len(self._slots),
                "max_connections": self.max_total_connections,
                "secondary_connections": len(self._slots),
                "active_channels": sum(slot.active_channels for slot in self._slots),
                "channel_capacity": len(self._slots) * _SSH_MAX_CHANNELS_PER_CONNECTION,
                "channels_per_connection": _SSH_MAX_CHANNELS_PER_CONNECTION,
                "scale_up_utilization": self.scale_up_utilization,
                "idle_timeout_seconds": self.idle_timeout_seconds,
                "closed": self._closed,
            }

    def close(self):
        with self._cond:
            if self._closed:
                return
            self._closed = True
            slots = list(self._slots)
            self._slots = []
            self._cond.notify_all()
        self._maintenance_stop.set()
        for slot in slots:
            self._close_quietly(slot.ssh)
        if self._maintenance_thread is not threading.current_thread():
            self._maintenance_thread.join(timeout=1.0)


def attach_ssh_connection_pool(ssh, host, port, user, pem="", password="",
                               max_total_connections=_SSH_POOL_MAX_CONNECTIONS):
    """Attach a lazy, autoscaling secondary pool to an existing SSH client."""
    pool = SSHConnectionPool(
        host, port, user, pem=pem, password=password,
        max_total_connections=max_total_connections,
    )
    setattr(ssh, _SSH_POOL_ATTR, pool)
    return pool


def close_ssh_connection_pool(ssh):
    pool = getattr(ssh, _SSH_POOL_ATTR, None) if ssh is not None else None
    if pool is not None:
        try:
            pool.close()
        except Exception:
            pass
        try:
            delattr(ssh, _SSH_POOL_ATTR)
        except Exception:
            pass


def _ssh_guard(ssh):
    """Fallback limiter for SSH clients not created by ConnectWorker."""
    guard = getattr(ssh, _SSH_GUARD_ATTR, None)
    if guard is None:
        guard = {
            "sem": threading.BoundedSemaphore(_SSH_MAX_CHANNELS_PER_CONNECTION),
            "lock": threading.RLock(),
        }
        try:
            setattr(ssh, _SSH_GUARD_ATTR, guard)
        except Exception:
            pass
    return guard


def open_managed_session(ssh, timeout=None):
    """Open a managed session channel.

    Preferred path: acquire a channel from the secondary SSH connection pool.
    Fallback path: use the supplied SSH client's own Transport with a bounded
    semaphore, preserving compatibility for externally-created SSH clients.

    timeout=None (the default) used to mean "wait forever" here — so if the
    pool was ever fully checked out (e.g. a channel leaked by some other
    caller, or just several features hammering it at once), any new file
    open/preview/command would sit spinning with no error and no way to
    tell why. It now falls back to a bounded default wait so a starved pool
    surfaces as a real "SSH is busy" error instead of an indefinite spinner.
    """
    if ssh is None:
        raise RuntimeError("SSH connection is not available")
    if timeout is None:
        timeout = _DEFAULT_SESSION_WAIT

    pool = getattr(ssh, _SSH_POOL_ATTR, None)
    if pool is not None:
        _owner_ssh, channel = pool.acquire_channel(timeout=timeout)
        return channel

    transport = ssh.get_transport()
    if transport is None or not transport.is_active():
        raise RuntimeError("SSH transport is not active")
    guard = _ssh_guard(ssh)
    acquired = guard["sem"].acquire(timeout=timeout) if timeout is not None else guard["sem"].acquire()
    if not acquired:
        raise RuntimeError("SSH is busy; no session channel is currently available")
    try:
        channel = transport.open_session(timeout=_CHANNEL_OPEN_TIMEOUT)
        channel._kdb_channel_slot = guard["sem"]
        return channel
    except Exception:
        guard["sem"].release()
        raise


def close_managed_session(channel):
    if channel is None:
        return
    pool = getattr(channel, "_kdb_pool_owner", None)
    try:
        channel.close()
    except Exception:
        pass
    finally:
        if pool is not None:
            pool.release_channel(channel)
        else:
            sem = getattr(channel, "_kdb_channel_slot", None)
            if sem is not None:
                try:
                    delattr(channel, "_kdb_channel_slot")
                except Exception:
                    pass
                try:
                    sem.release()
                except Exception:
                    pass


@contextmanager
def managed_exec_command(ssh, command, **kwargs):
    """Execute a command while reserving one bounded SSH session channel.
    stdout/stderr/stdin and the underlying channel are always closed."""
    channel = open_managed_session(ssh, timeout=kwargs.pop("channel_timeout", None))
    stdin = stdout = stderr = None
    try:
        if "get_pty" in kwargs and kwargs.pop("get_pty"):
            channel.get_pty()
        if "environment" in kwargs and kwargs["environment"]:
            channel.update_environment(kwargs["environment"])
        channel.exec_command(command)
        stdin = channel.makefile_stdin("wb", -1)
        stdout = channel.makefile("rb", -1)
        stderr = channel.makefile_stderr("rb", -1)
        yield stdin, stdout, stderr
    finally:
        for stream in (stdin, stdout, stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass
        close_managed_session(channel)


def track_worker(pool: list, worker: QThread) -> QThread:
    """Register *worker* in *pool* and auto-remove it once it finishes.

    Every call site that fires off a background worker needs to keep a
    reference to it (so it isn't garbage-collected mid-run) and drop that
    reference again once it's done. That add/remove bookkeeping used to be
    hand-rolled at each call site (a local ``on_finished`` closure, or a
    one-off lambda) with slightly different implementations scattered
    across main_window.py, dialogs.py, and kubernetes_tab.py. Centralizing
    it here keeps the behavior identical everywhere and removes the
    duplication. Returns the worker so it can be used as
    ``worker = track_worker(self._workers, CommandWorker(...))``.
    """
    pool.append(worker)
    worker.finished.connect(lambda: pool.remove(worker) if worker in pool else None)
    return worker