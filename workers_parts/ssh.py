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

_SSH_POOL_MAX_CONNECTIONS = 3
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


class SSHConnectionPool:
    """Lazily creates a small pool of secondary SSH connections.

    The original/main ``SSHClient`` remains the owner's primary connection
    (SFTP + heartbeat). This pool is used only by short-lived exec/stream
    channels. Each secondary connection is independently capped below a
    typical sshd ``MaxSessions`` value, and every channel releases its slot
    when closed.

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

    def __init__(self, host, port, user, pem="", password="", max_connections=_SSH_POOL_MAX_CONNECTIONS):
        self.host = host
        self.port = port
        self.user = user
        self.pem = pem or ""
        self.password = password or ""
        self.max_connections = max(1, int(max_connections))
        self._slots = []
        self._creating = False
        self._closed = False
        self._fail_until = 0.0
        self._fail_exc = None
        self._cond = threading.Condition(threading.RLock())

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
    def _find_slot_with_capacity(self, dead):
        """Return a live slot with a free channel (reserving it), else None.

        Slots whose Paramiko transport is inactive are removed from the pool
        and appended to ``dead`` so the caller can close them *after*
        releasing the lock. A pooled connection can die while KubeDock is
        idle; never hand out a channel from a stale transport.
        """
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
            if slot.sem.acquire(False):
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

                    slot = self._find_slot_with_capacity(dead)
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
                try:
                    slot.sem.release()
                except Exception:
                    pass
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
        try:
            slot.sem.release()
        except Exception:
            pass
        with self._cond:
            self._cond.notify_all()

    def close(self):
        with self._cond:
            self._closed = True
            slots = list(self._slots)
            self._slots = []
            self._cond.notify_all()
        for slot in slots:
            self._close_quietly(slot.ssh)


def attach_ssh_connection_pool(ssh, host, port, user, pem="", password=""):
    """Attach a lazy secondary-connection pool to an existing SSH client."""
    pool = SSHConnectionPool(host, port, user, pem=pem, password=password)
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