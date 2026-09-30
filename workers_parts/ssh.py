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
        self._cond = threading.Condition(threading.RLock())

    def _connect_secondary(self):
        import paramiko

        ssh = paramiko.SSHClient()
        # Keep the same host-key behavior as the existing primary connection
        # so introducing the pool does not change connection semantics.
        _configure_host_key_policy(ssh)
        kw = dict(
            hostname=self.host,
            port=self.port,
            username=self.user,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
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

    def _discard_dead_slots(self):
        """Remove pooled SSH connections whose Paramiko transport is inactive."""
        dead = []
        for slot in list(self._slots):
            try:
                transport = slot.ssh.get_transport()
                active = transport is not None and transport.is_active()
            except Exception:
                active = False
            if not active:
                dead.append(slot)

        if not dead:
            return

        for slot in dead:
            try:
                self._slots.remove(slot)
            except ValueError:
                continue
            try:
                slot.ssh.close()
            except Exception:
                pass

    def _find_slot_with_capacity(self):
        # A pooled connection can die while KubeDock is idle. Never hand out
        # a channel from a stale Paramiko transport; remove it so the normal
        # connection-creation path can replace it.
        self._discard_dead_slots()
        for slot in self._slots:
            try:
                transport = slot.ssh.get_transport()
                if transport is None or not transport.is_active():
                    continue
            except Exception:
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
        try:
            slot.ssh.close()
        except Exception:
            pass

    def acquire_channel(self, timeout=None):
        """Return ``(secondary_ssh, channel)`` and reserve one channel slot."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._cond:
                if self._closed:
                    raise RuntimeError("SSH connection pool is closed")

                slot = self._find_slot_with_capacity()
                if slot is not None:
                    break

                if len(self._slots) < self.max_connections and not self._creating:
                    self._creating = True
                    create = True
                else:
                    create = False
                    remaining = None if deadline is None else max(0, deadline - time.monotonic())
                    if remaining == 0:
                        raise RuntimeError("SSH is busy; no pooled session channel is currently available")
                    self._cond.wait(remaining)
                    continue

            if create:
                new_slot = None
                try:
                    secondary = self._connect_secondary()
                    new_slot = _SSHPoolSlot(secondary)
                    # Reserve the first channel for this caller.
                    new_slot.sem.acquire()
                    with self._cond:
                        if self._closed:
                            try:
                                secondary.close()
                            except Exception:
                                pass
                            new_slot = None
                        else:
                            self._slots.append(new_slot)
                            slot = new_slot
                except Exception:
                    if new_slot is not None:
                        try:
                            new_slot.ssh.close()
                        except Exception:
                            pass
                    with self._cond:
                        self._creating = False
                        self._cond.notify_all()
                    raise
                finally:
                    with self._cond:
                        self._creating = False
                        self._cond.notify_all()
                if slot is not None:
                    break

        try:
            transport = slot.ssh.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("Pooled SSH connection is no longer active")
            channel = transport.open_session()
            channel._kdb_pool_slot = slot
            channel._kdb_pool_owner = self
            return slot.ssh, channel
        except Exception:
            try:
                slot.sem.release()
            except Exception:
                pass
            # If opening a channel failed because this pooled connection died,
            # evict it immediately. The next acquire will create a fresh
            # secondary connection instead of repeatedly reusing the dead one.
            try:
                transport = slot.ssh.get_transport()
                dead = transport is None or not transport.is_active()
            except Exception:
                dead = True
            if dead:
                self._remove_slot(slot)
            with self._cond:
                self._cond.notify_all()
            raise

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
            try:
                slot.ssh.close()
            except Exception:
                pass


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
        channel = transport.open_session()
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
