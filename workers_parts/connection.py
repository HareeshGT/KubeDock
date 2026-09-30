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

from .ssh import _configure_host_key_policy, _configure_transport, attach_ssh_connection_pool, managed_exec_command

class ConnectWorker(QThread):
    """Establishes an SSH connection (and opens SFTP) in a background thread
    so the UI thread — and any 'Connecting…' animation — keeps running
    smoothly instead of freezing for the duration of the handshake."""

    connected = pyqtSignal(object, object, str)   # ssh, sftp, home_dir
    error     = pyqtSignal(str)

    def __init__(self, host, port, user, pem, password):
        # type: (str, int, str, str, str) -> None
        super().__init__()
        self.host     = host
        self.port     = port
        self.user     = user
        self.pem      = pem
        self.password = password
        self.finished.connect(self.deleteLater)

    def run(self):
        import paramiko
        try:
            ssh = paramiko.SSHClient()
            _configure_host_key_policy(ssh)
            kw = dict(hostname=self.host, port=self.port, username=self.user,
                      timeout=10, banner_timeout=10, auth_timeout=10)
            if self.password:
                # A real password was typed in — force straight password
                # auth. Left at paramiko's defaults, look_for_keys/
                # allow_agent are both True, so connect() tries every key
                # in ~/.ssh and every identity in a running ssh-agent
                # BEFORE ever trying this password — and if the server
                # hard-rejects one of those (a passphrase-locked key it
                # can't open, a key the server explicitly refuses, etc.),
                # paramiko raises AuthenticationException right there and
                # the password is never attempted. This is why "connect
                # with password" could fail even with the correct password
                # typed in. Forcing both off makes this a real password-
                # only attempt, exactly like ssh -o
                # PreferredAuthentications=password would.
                kw["password"]      = self.password
                kw["look_for_keys"] = False
                kw["allow_agent"]   = False
            elif self.pem:
                kw["key_filename"] = self.pem
            elif self.host in ["127.0.0.1", "localhost"]:
                # "Connect to Localhost" quick-button with no password/pem
                # entered — leave look_for_keys/allow_agent at their
                # defaults so a local ~/.ssh key or running ssh-agent can
                # still authenticate, same as a bare `ssh user@localhost`.
                pass
            ssh.connect(**kw)

            # Paramiko's default per-channel flow-control window is small
            # (~2MB), which caps throughput hard on a fast link — every
            # read/write ends up round-trip-bound rather than actually
            # using the available bandwidth. Widen it before any channel
            # (SFTP or exec) is opened on this transport, so every channel
            # opened afterwards inherits it.
            #
            # NOTE: this must be a large-but-sane value, not the literal
            # protocol maximum (paramiko.common.MAX_WINDOW_SIZE, 2**32-1).
            # A lot of real-world SSH servers, appliances, and firewalls
            # reject or mishandle a window advertisement that large and
            # just reset the connection outright — which showed up as
            # instant "Error: Socket is closed" upload/download failures.
            # 64MB is the same order of magnitude used by other
            # high-throughput SFTP clients and is safe in practice.
            try:
                transport = ssh.get_transport()
                transport.default_window_size = 64 * 1024 * 1024

                # Paramiko renegotiates the session keys (a "rekey") once a
                # certain amount of data has crossed the transport — by
                # default around 1GB (packetizer.REKEY_BYTES) or a large
                # packet count (REKEY_PACKETS), whichever comes first. While
                # that renegotiation is happening the *entire* transport is
                # paused — every channel on it, including whatever SFTP
                # transfer is mid-flight — until the handshake completes.
                #
                # Crucially, this counter is cumulative for the *whole life
                # of the transport*, not per-transfer: one SSH/SFTP
                # connection is kept open and shared across everything the
                # app does in a session (directory listings, previews,
                # terminal commands, k8s polling, earlier up/downloads, ...).
                # So even a modest file can be the transfer that happens to
                # tip the counter over, if earlier session activity already
                # used up most of the threshold — that's exactly the
                # "stalls even on a 170MB file" symptom. Raising the
                # threshold well past anything a normal session is likely to
                # accumulate means this basically never fires on its own;
                # a server-initiated rekey (if the remote side enforces one)
                # is unaffected by this and still happens normally.
                try:
                    transport.packetizer.REKEY_BYTES   = pow(2, 40)  # ~1TB
                    transport.packetizer.REKEY_PACKETS = pow(2, 40)
                except Exception:
                    pass

                # Paramiko never sends anything on its own while a session
                # sits idle. Some time after connecting — browsing around,
                # picking a big file to upload — the remote sshd's
                # ClientAliveInterval, a NAT gateway, a cloud load
                # balancer, or a firewall's idle-connection timeout can
                # silently drop the session. The socket then just sits
                # there looking "open" until the next real read/write,
                # which is exactly when an upload/download would hit an
                # instant "Socket is closed" at 0 bytes. A periodic
                # SSH-level keepalive (a lightweight global request every
                # 15s) keeps NAT/firewall mappings alive and lets a truly
                # dead connection be detected quickly instead of silently.
                transport.set_keepalive(15)
            except Exception:
                pass

            sftp = ssh.open_sftp()
            with managed_exec_command(ssh, "echo $HOME") as (_stdin, stdout, _stderr):
                home = stdout.read().decode().strip()

            # Keep this primary SSH connection for SFTP/health checks and
            # attach a lazy pool for all short-lived command/stream work.
            # Secondary connections are only created when concurrency actually
            # requires them.
            effective_pem = self.pem if not self.password else ""
            attach_ssh_connection_pool(
                ssh, self.host, self.port, self.user,
                pem=effective_pem, password=self.password
            )
            self.connected.emit(ssh, sftp, home)
        except Exception as e:
            try:
                if 'sftp' in locals() and sftp:
                    sftp.close()
                if 'ssh' in locals() and ssh:
                    ssh.close()
            except Exception:
                pass
            self.error.emit(str(e))


class FTPConnectionWorker(QThread):
    """Connects to FTP or explicit FTPS without blocking the Qt UI."""
    connected = pyqtSignal(object, str)   # FTPFS, initial path
    error = pyqtSignal(str)

    def __init__(self, host, port, user, password, tls=False, passive=True):
        super().__init__()
        self.host, self.port, self.user, self.password = host, port, user, password
        self.tls, self.passive = tls, passive
        self.finished.connect(self.deleteLater)

    def run(self):
        fs = None
        try:
            from ftp_fs import FTPFS
            fs = FTPFS(self.host, self.port, self.user, self.password, tls=self.tls, passive=self.passive)
            self.connected.emit(fs, fs.normalize("/"))
        except Exception as e:
            if fs is not None:
                try: fs.close()
                except Exception: pass
            self.error.emit(str(e))


class ConnectionHealthWorker(QThread):
    """Watches the live SSH transport and warns the UI before the session
    is actually gone, instead of the app only finding out when the next
    real operation (file listing, command, tunnel restart...) fails.

    ConnectWorker already calls transport.set_keepalive(15), but that runs
    entirely inside paramiko's own background thread and gives the app no
    visible signal either way — it silently keeps the socket alive, or
    silently lets it die. This worker adds an observable heartbeat on top:
    every INTERVAL seconds it checks transport.is_active() and sends a
    harmless SSH-level "ignore" packet, on a background thread so it never
    blocks the UI. One missed heartbeat is reported as `at_risk` (an early
    warning — could be a transient blip); LOST_THRESHOLD consecutive
    misses, or transport.is_active() going False outright, is reported as
    `lost`.
    """

    at_risk   = pyqtSignal(str)   # first missed heartbeat — may be transient
    recovered = pyqtSignal()      # heartbeats resumed after being at_risk
    lost      = pyqtSignal(str)   # transport confirmed dead

    INTERVAL       = 10   # seconds between heartbeats
    LOST_THRESHOLD = 3    # consecutive missed heartbeats before declaring it lost
    # How long one round-trip probe may stay unanswered before it counts as
    # a miss. Deliberately generous: the probe queues behind any bulk SFTP
    # traffic on the same transport, and a slow link mid-upload must not be
    # mistaken for a dead one.
    PROBE_TIMEOUT  = 20

    def __init__(self, ssh):
        super().__init__()
        self.ssh = ssh
        self._stop = threading.Event()
        self._probe_thread = None
        self.finished.connect(self.deleteLater)

    def stop(self):
        # Unblocks the wait() below immediately so the loop exits before
        # its next heartbeat, instead of firing one more check (and
        # possibly a stray `lost` signal) after the caller has already
        # torn down the connection on purpose.
        self._stop.set()

    def _probe(self, transport):
        """Real SSH round trip: True only if the server *answered*.

        ``send_ignore()`` alone only proves the local socket accepted bytes.
        After a NAT/VPN drop or a laptop sleep/wake the connection is
        half-open: writes keep "succeeding" into the kernel buffer for many
        minutes while nothing ever comes back, so the session looked healthy
        right up until the next real operation hung. A global request needs
        a reply (OpenSSH answers ``keepalive@openssh.com``; servers that
        don't know it answer REQUEST_FAILURE — either counts as alive).

        paramiko's ``global_request`` has no timeout of its own, so it runs
        in a helper thread and is abandoned after PROBE_TIMEOUT. Only one
        probe is ever outstanding; a still-unanswered previous probe counts
        as another miss rather than stacking a new request.
        """
        prev = self._probe_thread
        if prev is not None and prev.is_alive():
            return False

        result = {}

        def _go():
            try:
                transport.global_request("keepalive@openssh.com", wait=True)
                result["ok"] = True
            except Exception as exc:
                result["err"] = exc

        t = threading.Thread(target=_go, name="kdb-ssh-probe", daemon=True)
        self._probe_thread = t
        t.start()
        # Poll in short slices so stop() is still honoured promptly.
        waited = 0.0
        while t.is_alive() and waited < self.PROBE_TIMEOUT and not self._stop.is_set():
            t.join(0.25)
            waited += 0.25
        if result.get("err") is not None:
            raise result["err"]
        return bool(result.get("ok")) and transport.is_active()

    def run(self):
        fail_count   = 0
        was_at_risk  = False
        while not self._stop.wait(self.INTERVAL):
            try:
                transport = self.ssh.get_transport()
            except Exception:
                transport = None

            if transport is None or not transport.is_active():
                self.lost.emit("SSH transport is no longer active.")
                return

            try:
                transport.send_ignore()
                if not self._probe(transport):
                    if self._stop.is_set():
                        return
                    raise RuntimeError(
                        "No response from server to keepalive within {}s".format(self.PROBE_TIMEOUT)
                    )
            except Exception as e:
                if self._stop.is_set():
                    return
                fail_count += 1
                if fail_count >= self.LOST_THRESHOLD:
                    self.lost.emit(str(e))
                    return
                if fail_count == 1:
                    self.at_risk.emit(str(e))
                    was_at_risk = True
            else:
                if was_at_risk:
                    self.recovered.emit()
                    was_at_risk = False
                fail_count = 0