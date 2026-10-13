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

from .ssh import open_managed_session, close_managed_session
from elevated_read import ElevatedReadError, download_elevated, is_permission_denied

class FileStreamReadWorker(QThread):
    """Reads a remote file in 64KB chunks on a background QThread.

    Opening/editing a large remote file used to do a single synchronous
    ``sftp.open(path).read(N)`` (or, worse, a sudo ``cat`` whose *entire*
    output was buffered by ``SudoFS._run()``) directly on the UI thread.
    For a big enough file that call could take long enough to look like a
    hang, and for the sudo path it could pull the whole file into memory
    before any size cap even applied — together, effectively a crash for
    large files. This worker moves that read off the UI thread entirely
    and reads incrementally, chunk by chunk, so the Qt event loop keeps
    running (progress can be shown, Cancel stays clickable) no matter how
    large the remote file is.

    Works for both the plain-SFTP path and the sudo path:
      - No sudo user: reads straight from the paramiko SFTP file handle,
        which gives real per-chunk progress against the file's actual size.
      - Sudo user active: streams a ``sudo -u <user> cat`` command's output
        over a raw paramiko channel (recv() polling), the same pattern
        used elsewhere in the app for live command streaming, rather than
        letting it block until the whole thing lands in one shot.
    """

    progress     = pyqtSignal(int, int)   # bytes_done, bytes_total (total==0 if unknown)
    chunk_ready  = pyqtSignal(bytes)      # each chunk, as it arrives — lets a caller render live
    finished_ok  = pyqtSignal(int)        # total bytes read, once the read completes (or hits max_bytes)
    finished_err = pyqtSignal(str)

    CHUNK_SIZE = 64 * 1024

    def __init__(self, sftp, ssh, remote_path, sudo_user=None, max_bytes=None):
        # type: (object, object, str, Optional[str], Optional[int]) -> None
        super().__init__()
        self._sftp      = sftp   # SudoFS instance
        self._ssh       = ssh
        self._remote     = remote_path
        self._sudo_user  = sudo_user
        self._max_bytes  = max_bytes
        self._cancelled  = False
        # Handle to whatever blocking resource the active _run_*_stream
        # is currently reading from (a paramiko SFTPFile or raw Channel),
        # so cancel() can close it out from under a blocked read/recv
        # call instead of only flipping a flag. Without this, a worker
        # blocked inside a single paramiko read (e.g. the preview fetch
        # that a double-click's own edit-load races right behind — see
        # _cancel_preview_worker's docstring in main_window.py) keeps
        # holding the shared SFTP connection until that one call happens
        # to return on its own, and a second worker starting concurrently
        # against the same connection in the meantime can wedge both.
        self._handle    = None
        self.finished.connect(self.deleteLater)

    def cancel(self):
        self._cancelled = True
        handle = self._handle
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def run(self):
        try:
            if hasattr(self._sftp, "_ftp"):
                self._run_ftp_stream()
            elif self._sudo_user:
                self._run_sudo_stream()
            else:
                self._run_direct_stream()
        except Exception as e:
            self.finished_err.emit(str(e))

    def _run_ftp_stream(self):
        try:
            total = self._sftp.stat(self._remote).st_size
        except Exception:
            total = 0
        done = 0
        # Use a dedicated per-transfer connection (FTPFS.open_stream()),
        # not self._sftp._ftp — that's the one shared control connection
        # the whole app uses for browsing/other operations. Calling
        # retrbinary() directly on it meant that closing this dialog and
        # reopening the same file fast enough left the old worker's
        # transfer still running against that shared connection when the
        # new worker's transfer started on it too: both workers' command/
        # response traffic interleaved on the same socket, which is what
        # produced garbage content on reopen. A dedicated connection per
        # worker also means cancel() (below) can actually stop an
        # in-flight read by closing this worker's own handle, instead of
        # having nothing to close and leaving the transfer running.
        reader = self._sftp.open_stream(self._remote)
        self._handle = reader
        try:
            while True:
                if self._cancelled or (self._max_bytes and done >= self._max_bytes):
                    break
                try:
                    data = reader.read(self.CHUNK_SIZE)
                except Exception:
                    if self._cancelled:
                        break
                    raise
                if not data:
                    break
                take = data
                if self._max_bytes:
                    take = data[:max(0, self._max_bytes - done)]
                if take:
                    done += len(take)
                    self.chunk_ready.emit(bytes(take))
                    self.progress.emit(done, total)
        finally:
            self._handle = None
            try:
                reader.close()
            except Exception:
                pass
        if not self._cancelled:
            self.finished_ok.emit(done)

    # ── plain SFTP path — real chunked reads with real progress ──
    def _run_direct_stream(self):
        # Open a dedicated SFTP channel on the same SSH transport rather
        # than reusing self._sftp._sftp — the one paramiko SFTPClient
        # every other reader (the preview pane, another open editor)
        # also reads through. paramiko's SFTPClient is documented as not
        # safe for concurrent use from more than one thread (see
        # _SFTPStreamReader above, which already opens its own
        # ssh.open_sftp() per reader for media playback for exactly this
        # reason): two workers issuing requests on the same client at
        # once can interleave their wire packets, and paramiko raises
        # "Garbage packet received" trying to parse the result. That's
        # what happens when a file's still loading in the preview pane
        # and gets double-clicked to edit before _cancel_preview_worker's
        # cancel() has actually stopped that thread — both workers end up
        # on the shared client at the same time. LocalBackend has no SSH
        # transport (self._ssh is None there), so it keeps using the
        # passed-in filesystem object directly, unchanged.
        dedicated = self._ssh is not None and hasattr(self._sftp, "_sftp")
        raw = self._ssh.open_sftp() if dedicated else getattr(self._sftp, "_sftp", self._sftp)
        try:
            total = raw.stat(self._remote).st_size
        except Exception:
            total = 0

        done = 0
        f = raw.open(self._remote, "rb")
        self._handle = f
        try:
            # NOTE: no f.prefetch() here. prefetch() fires a burst of
            # concurrent async SFTP requests ahead of the sequential
            # read() calls below, and on a freshly-opened SFTP channel
            # (e.g. the first file opened right after connecting) a
            # response can land against the wrong pending request in
            # paramiko's bookkeeping, leaving a later read() waiting on
            # a request ID that never resolves — a silent, permanent
            # stall with the server sitting idle. Plain sequential
            # read(CHUNK_SIZE) below never has more than one request
            # outstanding, so there's nothing to mis-file.
            while True:
                if self._cancelled:
                    return
                try:
                    buf = f.read(self.CHUNK_SIZE)
                except Exception:
                    # cancel() closing the handle out from under us
                    # surfaces here as a read error — treat that as a
                    # clean stop rather than a load failure.
                    if self._cancelled:
                        return
                    raise
                if not buf:
                    break
                done += len(buf)
                self.chunk_ready.emit(buf)
                self.progress.emit(done, total)
                if self._max_bytes and done >= self._max_bytes:
                    break
        finally:
            self._handle = None
            try:
                f.close()
            except Exception:
                pass
            if dedicated:
                try:
                    raw.close()
                except Exception:
                    pass
        self.finished_ok.emit(done)

    # ── sudo path — stream a "sudo -u <user> cat" over a raw channel ──
    def _run_sudo_stream(self):
        try:
            total = self._sftp.stat(self._remote).st_size
        except Exception:
            total = 0

        prefix = getattr(self._sftp, "_sudo_prefix", "")
        sq = getattr(self._sftp, "_sq", lambda p: "'" + p.replace("'", "'\\''") + "'")
        cmd = "{}cat {} 2>/dev/null".format(prefix, sq(self._remote))
        channel = open_managed_session(self._ssh)
        self._handle = channel
        try:
            channel.settimeout(0.5)
            channel.exec_command(cmd)
            done = 0
            while True:
                if self._cancelled:
                    return
                try:
                    if channel.recv_ready():
                        buf = channel.recv(self.CHUNK_SIZE)
                        if buf:
                            done += len(buf)
                            self.chunk_ready.emit(buf)
                            self.progress.emit(done, total)
                            if self._max_bytes and done >= self._max_bytes:
                                return
                            continue
                except Exception:
                    pass
                if channel.exit_status_ready() and not channel.recv_ready():
                    break
                self.msleep(30)
            self.finished_ok.emit(done)
        finally:
            self._handle = None
            close_managed_session(channel)


class _TransferWorker(QThread):
    """Runs a single SFTP get/put in a background thread and emits progress."""

    progress     = pyqtSignal(int, int)   # bytes_done, bytes_total
    finished_ok  = pyqtSignal()
    finished_err = pyqtSignal(str)

    def __init__(self, sftp, direction, local_path, remote_path):
        # type: (object, str, str, str) -> None
        super().__init__()
        self._sftp      = sftp           # SudoFS instance
        self._direction = direction      # "upload" | "download"
        self._local     = local_path
        self._remote    = remote_path
        self._cancelled = False
        self.finished.connect(self.deleteLater)

    def cancel(self):
        self._cancelled = True

    def run(self):
        try:
            if self._direction == "download":
                self._download()
            else:
                self._upload()
            if not self._cancelled:
                self.finished_ok.emit()
        except Exception as e:
            if not self._cancelled:
                msg = str(e)
                # A dead/idled-out SSH session (no keepalive reached it in
                # time, the server's ClientAliveInterval fired, a NAT/
                # firewall dropped it, ...) surfaces here as a low-level
                # socket error on the very first read/write — the raw
                # message ("Socket is closed", "Connection reset by
                # peer") isn't obviously actionable, so translate it.
                low = msg.lower()
                if "socket is closed" in low or "connection reset" in low or "broken pipe" in low:
                    msg = (
                        "Connection to the VM was lost (the SSH session "
                        "went idle and got dropped). Please reconnect and "
                        "try again."
                    )
                self.finished_err.emit(msg)

    # ── helpers ──────────────────────────────────────────────
    def _download(self):
        if hasattr(self._sftp, "_ftp"):
            try: total = self._sftp.stat(self._remote).st_size
            except Exception: total = 0
            done = [0]
            with open(self._local, "wb") as f:
                def cb(data):
                    if self._cancelled: return
                    f.write(data); done[0] += len(data); self.progress.emit(done[0], total)
                self._sftp._ftp.retrbinary("RETR " + self._sftp.normalize(self._remote), cb, blocksize=256*1024)
            return
        if self._sftp.sudo_user:
            # Explicit sudo-user mode: every read already runs as that user.
            # (The previous body unpacked SudoFS._run()'s 3-tuple as 2 values
            # — a ValueError on every call — and round-tripped the bytes
            # through str, which corrupts binary files. Stream raw bytes.)
            try:
                total = self._sftp.stat(self._remote).st_size
            except Exception:
                total = 0
            self._download_elevated(self._sftp.sudo_user, total)
        else:
            # Direct SFTP with real progress.
            #
            # The naive version of this loop (a plain remote_f.read(chunk)
            # in a while loop) issues one SFTP request, waits for its
            # reply, *then* issues the next — every chunk pays a full
            # network round trip before the next one even starts. On a
            # fast/high-latency-ish link that caps throughput at a few
            # hundred KB/s no matter how much raw bandwidth is actually
            # available (exactly the "1400MiB/s on the box, KB/s over
            # SFTP" symptom). prefetch() queues many concurrent read
            # requests in the background so .read() calls are mostly
            # served from an already-filled buffer instead of blocking on
            # a round trip each time — this is the same technique
            # paramiko's own sftp.get() uses internally.
            REQUEST_SIZE = 256 * 1024
            # Only the *remote* stat/open is guarded: a remote
            # permission-denied here (unreadable file or non-searchable
            # parent dir) is retried once as ``sudo -n cat``. Anything else
            # — not found, network/SSH failure, timeout — is re-raised
            # untouched, and a local write error below never escalates.
            try:
                total    = self._sftp._sftp.stat(self._remote).st_size
                remote_f = self._sftp._sftp.open(self._remote, "rb")
            except Exception as exc:
                if (is_permission_denied(exc)
                        and getattr(self._sftp, "_ssh", None) is not None):
                    self._download_elevated(None, 0, exc)
                    return
                raise
            done  = 0
            chunk = REQUEST_SIZE
            with remote_f:
                remote_f.MAX_REQUEST_SIZE = REQUEST_SIZE
                remote_f.prefetch(total)
                with open(self._local, "wb") as local_f:
                    while True:
                        if self._cancelled:
                            return
                        buf = remote_f.read(chunk)
                        if not buf:
                            break
                        local_f.write(buf)
                        done += len(buf)
                        self.progress.emit(done, total)

    def _download_elevated(self, sudo_user, total, original_exc=None):
        """Read the remote file with ``sudo -n cat`` (see elevated_read.py)."""
        try:
            download_elevated(
                self._sftp._ssh, self._remote, self._local,
                sudo_user=sudo_user, total=total,
                on_progress=lambda d, t: self.progress.emit(d, t),
                is_cancelled=lambda: self._cancelled,
            )
        except ElevatedReadError as err:
            raise IOError(str(err))

    def _upload(self):
        if hasattr(self._sftp, "_ftp"):
            total = os.path.getsize(self._local)
            done = [0]

            with open(self._local, "rb") as f:
                def cb(data):
                    if self._cancelled:
                        return
                    done[0] += len(data)
                    self.progress.emit(done[0], total)

                self._sftp._ftp.storbinary(
                    "STOR " + self._sftp.normalize(self._remote),
                    f,
                    blocksize=256 * 1024,
                    callback=cb,
                )
            return

        # ── SFTP / sudo upload ───────────────────────────────────
        # NOTE: this whole branch went missing when the FTP branch above
        # was added (commit 210bf0c replaced the body of _upload with only
        # the FTP code). Every non-FTP upload that reached this worker —
        # any sudo-user upload, and any password login where scp can't be
        # used (Windows) — ran zero code, returned normally, and
        # _TransferWorker.run() then emitted finished_ok: "Done" with
        # nothing uploaded.
        total = os.path.getsize(self._local)
        chunk = 65536
        done  = 0
        if self._sftp.sudo_user:
            self.progress.emit(0, total)
            self._sftp.put(self._local, self._remote)
            self.progress.emit(total, total)
        else:
            # Same round-trip problem as the download path, mirrored for
            # writes: set_pipelined(True) stops paramiko from waiting for
            # each write's server ack before sending the next chunk, so
            # writes queue up back-to-back instead of stalling on
            # latency. This is what paramiko's own sftp.put() does
            # internally, too.
            # 32KB, NOT 256KB: OpenSSH's sftp-server caps a whole SFTP
            # message at 256KiB (header included), so a 256KiB WRITE
            # payload is over the limit and the server just drops the
            # connection — surfacing as an instant "Socket is closed"
            # at 0 bytes. 32KB is what OpenSSH's own client uses and
            # works on every server; pipelining below keeps it fast.
            REQUEST_SIZE = 32 * 1024
            chunk = REQUEST_SIZE
            with open(self._local, "rb") as local_f:
                with self._sftp._sftp.open(self._remote, "wb") as remote_f:
                    remote_f.MAX_REQUEST_SIZE = REQUEST_SIZE
                    remote_f.set_pipelined(True)
                    while True:
                        if self._cancelled:
                            return
                        buf = local_f.read(chunk)
                        if not buf:
                            break
                        remote_f.write(buf)
                        done += len(buf)
                        self.progress.emit(done, total)

        # Never report success on faith: confirm the file is really there
        # and the right size. (A silent no-op like the one above would have
        # been caught by this immediately.)
        try:
            remote_size = self._sftp.stat(self._remote).st_size
        except Exception:
            remote_size = None   # can't verify on this server — don't fail a good upload
        if remote_size is not None and remote_size != total:
            raise IOError(
                "Upload verification failed: remote file is {} bytes, expected {}.".format(
                    remote_size, total)
            )


class _PtyProc:
    """Wraps an os.forkpty() child so the read loop below can treat it the
    same way it treats a subprocess.Popen object — .poll() / .wait() /
    .kill(), nothing loop-specific has to know which one it's holding."""

    def __init__(self, pid: int, master_fd: int):
        self.pid       = pid
        self.master_fd = master_fd
        self._status   = None

    def fileno(self) -> int:
        return self.master_fd

    def poll(self):
        """None while still running, else the exit code."""
        if self._status is not None:
            return self._status
        try:
            wpid, status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            self._status = 0
            return self._status
        if wpid == 0:
            return None
        self._status = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        return self._status

    def wait(self):
        if self._status is not None:
            return self._status
        try:
            _, status = os.waitpid(self.pid, 0)
        except ChildProcessError:
            self._status = 0
            return self._status
        self._status = os.WEXITSTATUS(status) if os.WIFEXITED(status) else 1
        return self._status

    def kill(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
        except Exception:
            pass


class ScpTransferWorker(QThread):
    """Runs a single upload/download as a real ``scp`` subprocess on the
    machine this app is running on, instead of shuttling bytes through
    paramiko's SFTP implementation.

    Why this exists: paramiko's SFTP — even with the prefetch/pipelining
    tricks in _TransferWorker above — tops out well short of what the same
    link does over a plain ``scp``: OpenSSH's own C implementation
    pipelines far more aggressively and isn't paying Python's per-packet
    overhead. Shelling out to the system ``scp`` binary gets the same
    throughput a user would get typing the command by hand, e.g.:

        scp -i key.pem ~/Downloads/4K.mp4 ec2-user@44.224.86.255:/home/ec2-user/gt/

    This is only used for the plain (non-sudo) case — a sudo-target
    upload/download still goes through SudoFS/_TransferWorker, since that
    path already needs a second hop (upload to a tmp path, then ``sudo mv``
    over the existing ssh session) that a single scp invocation can't
    express. Both key- and password-authenticated connections use scp:
    for a password, this worker answers scp's own "password:" prompt
    itself over the pty below, the same way a person typing it by hand
    would — no external helper (sshpass, etc.) needed.
    """

    progress     = pyqtSignal(int, int)   # bytes_done (estimated from scp's own % ), bytes_total
    finished_ok  = pyqtSignal()
    finished_err = pyqtSignal(str)

    _PASSWORD_PROMPT_RE = re.compile(rb"(?i)password:\s*$")

    # Any *other* interactive prompt scp can stop on (key passphrase, a
    # 2FA "Verification code:", a host-key "(yes/no)?", a second
    # "password:" after ours was already sent). We can't answer these, and
    # with no timeout the transfer would sit there forever with no
    # feedback, so run() aborts on them and reports the prompt text.
    _OTHER_PROMPT_RE = re.compile(
        rb"(?i)(passphrase|verification code|one-time|otp|\(yes/no[^)]*\)\??|password)[^\r\n]*[:?]\s*$"
    )
    _PROGRESS_LINE_RE = re.compile(r"\d{1,3}%\s+\S+\s+\S+/s")

    @classmethod
    def _summarize_output(cls, raw, ret):
        """Turn scp/ssh's raw pty output into a short, human-readable error.

        The progress meter and the echoed password prompt are dropped, so
        what's left is the real reason: "Permission denied", "REMOTE HOST
        IDENTIFICATION HAS CHANGED", "No such file or directory", etc.
        """
        text = raw.decode(errors="replace")
        text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
        lines = []
        for seg in re.split(r"[\r\n]+", text):
            seg = seg.strip()
            if not seg or cls._PROGRESS_LINE_RE.search(seg):
                continue
            if seg.startswith("Warning: Permanently added"):
                continue
            if seg.lower().endswith("password:"):
                continue
            lines.append(seg)
        if lines:
            return " | ".join(lines[-4:])
        if ret == 127:
            return "The 'scp' command could not be started (not found on PATH)."
        return "scp exited with status {}".format(ret)

    @staticmethod
    def pty_available():
        """True when this OS can give scp a real pseudo-terminal
        (``os.forkpty`` — macOS/Linux). Windows has no equivalent."""
        return hasattr(os, "forkpty")

    @staticmethod
    def effective_auth(pem, password):
        """Return the (pem, password) pair scp must authenticate with.

        This mirrors ConnectWorker exactly: when a password was typed it
        wins and the pem is ignored (ConnectWorker connects password-only
        in that case). The transfer has to use the same credential that
        actually authenticated the SSH session; otherwise a connection
        that has both a pem and a password filled in browses fine over
        paramiko, but scp runs key-only (BatchMode) with a key the server
        never accepted and fails with an opaque "status 255".
        """
        if password:
            return "", password
        return (pem or ""), None

    @classmethod
    def supports(cls, pem, password):
        """Can this worker finish a transfer with these credentials
        *without ever asking the user for anything*?

        - Password auth: only if we have a pty to type the password into.
          Without one (Windows) nothing can answer scp's ``password:``
          prompt, so OpenSSH falls back to prompting on the *local
          console* — which is exactly the "asks for the remote machine's
          password" bug. Callers must use the SFTP path (which reuses the
          already-authenticated paramiko session) in that case.
        - Key auth: always (scp runs with BatchMode=yes, no prompts).
        """
        pem, password = cls.effective_auth(pem, password)
        if password:
            return cls.pty_available()
        return bool(pem)

    def __init__(self, host, port, user, pem, password, direction, local_path, remote_path, total_size=None):
        # type: (str, int, str, str, str, str, str, str, Optional[int]) -> None
        super().__init__()
        self._host      = host
        self._port      = port or 22
        self._user      = user
        self._pem, self._password = self.effective_auth(pem, password)
        self._direction = direction        # "upload" | "download"
        self._local     = local_path
        self._remote    = remote_path
        self._total     = total_size or 0
        self._cancelled = False
        self._proc      = None
        self.finished.connect(self.deleteLater)

    def cancel(self):
        self._cancelled = True
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass

    # ── helpers ──────────────────────────────────────────────
    def _build_cmd(self):
        remote_spec = "{}@{}:{}".format(self._user, self._host, self._remote)
        if self._direction == "upload":
            src, dst = self._local, remote_spec
        else:
            src, dst = remote_spec, self._local

        cmd = ["scp", "-o", "StrictHostKeyChecking=accept-new"]
        # accept-new auto-trusts a host key we haven't seen before but
        # still refuses one that *changed*, same as ssh would warn about
        # interactively — either way we're not left hanging on that prompt.
        if self._pem:
            # Fully unattended: BatchMode disables every interactive
            # prompt (falling straight to an error instead), which is
            # safe here since a key is what's actually authenticating.
            cmd += ["-o", "BatchMode=yes", "-i", self._pem]
        elif self._password:
            # We're about to answer scp's password prompt ourselves over
            # the pty (see run()), so BatchMode must stay off here. Skip
            # local ~/.ssh keys and any running ssh-agent identity so a
            # hard rejection from one of those can't derail this into a
            # failure before the password prompt even shows up — the same
            # class of bug fixed on the paramiko side in ConnectWorker.
            # NumberOfPasswordPrompts=1 means a wrong password fails fast
            # instead of scp silently re-prompting for a password we're
            # not going to answer again.
            cmd += ["-o", "PubkeyAuthentication=no", "-o", "NumberOfPasswordPrompts=1"]
        if self._port and int(self._port) != 22:
            cmd += ["-P", str(self._port)]
        cmd += [src, dst]
        return cmd

    @staticmethod
    def _parse_pct(line: bytes):
        """Pull the percentage out of one line/segment of scp's own
        progress meter, e.g. '4Kfile.mp4    45%   45MB   10.2MB/s   00:02'."""
        text = line.decode(errors="replace")
        m = re.search(r"(\d{1,3})%", text)
        if not m:
            return None
        return max(0, min(100, int(m.group(1))))

    def run(self):
        import subprocess
        import select

        if not self._total and self._direction == "upload":
            try:
                self._total = os.path.getsize(self._local)
            except Exception:
                self._total = 0

        # Safety net (dialogs.py already routes these to SFTP via
        # supports()): never launch scp for a password login when there's
        # no pty to answer its prompt — it would ask on the local console.
        if not self.supports(self._pem, self._password):
            self.finished_err.emit(
                "Password transfers via scp aren't supported on this OS; "
                "use the SFTP transfer path instead."
            )
            return

        cmd = self._build_cmd()

        # scp's progress meter is gated on more than just "is stdout a tty":
        # OpenSSH also checks that the process is running in the
        # *foreground* of that terminal's session (tcgetpgrp), which needs
        # a real controlling terminal, not just a tty-typed file descriptor.
        # Handing a plain pty slave fd to subprocess.Popen (via
        # pty.openpty()) gives scp a tty it can isatty()-check, but the
        # child is never made a session leader or given that pty as its
        # controlling terminal — so the foreground check still fails and
        # the meter stays off, exactly like a plain pipe. os.forkpty() is
        # the version that actually does the setsid()-and-attach dance (the
        # same mechanism the `script` command and tools like pexpect use),
        # so scp behaves exactly as it does when a person runs it by hand.
        pid       = None
        master_fd = None
        try:
            pid, master_fd = os.forkpty()
        except (AttributeError, OSError):
            pid = None

        if pid == 0:
            # Child: this thread's Python state ends here — replace the
            # process image immediately with the real scp binary. cmd was
            # already fully built before the fork, so there's nothing left
            # to compute (allocate/lock) in the child beforehand.
            try:
                os.execvp(cmd[0], cmd)
            finally:
                os._exit(127)  # only reached if execvp itself failed

        if pid is not None:
            proc    = _PtyProc(pid, master_fd)
            read_fd = master_fd
        else:
            # forkpty() unavailable for some reason — fall back to a plain
            # pipe. No live progress meter from scp in this case, but the
            # transfer and the exit-detection logic below still work.
            try:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                    # Windows: don't flash a console window from a GUI app.
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except FileNotFoundError:
                self.finished_err.emit(
                    "The 'scp' command isn't available on this machine. Install "
                    "an OpenSSH client (or check your network/PATH settings)."
                )
                return
            except Exception as e:
                self.finished_err.emit(str(e))
                return
            read_fd = proc.stdout.fileno()

        self._proc = proc  # so cancel() can kill whichever kind this is

        buf           = b""
        last_pct      = -1
        password_sent = not bool(self._password)
        # Bounded copy of everything scp/ssh printed. `buf` below is
        # consumed line by line to drive the progress meter, so by the
        # time scp exits it no longer holds the actual error text; this
        # log does, and is what the failure message is built from.
        log           = bytearray()
        prompt_abort  = None
        # Drive this off the process's own exit status (poll()), not off
        # read() returning EOF. ssh/scp can leave the pty/pipe's write end
        # referenced by a lingering child process even after the transfer
        # you actually care about has finished, so waiting for a real EOF
        # can hang indefinitely with the file already fully uploaded on the
        # other end. Polling for process exit alongside a short select()
        # timeout means we notice the moment scp itself is done, and only
        # keep reading in the meantime to surface live progress.
        while True:
            if self._cancelled:
                proc.kill()
                break

            exit_code = proc.poll()
            try:
                rlist, _, _ = select.select([read_fd], [], [], 0.2)
            except (OSError, ValueError):
                rlist = []
                if pid is None:
                    # Pipe path on Windows: select() can't poll a pipe, so
                    # without this the loop would busy-spin a CPU core.
                    time.sleep(0.2)

            got_data = False
            if rlist:
                try:
                    chunk = os.read(read_fd, 4096)
                except OSError:
                    # A pty raises EIO once the slave side is fully closed,
                    # instead of returning b"" like a pipe would — treat it
                    # the same as "nothing more to read right now".
                    chunk = b""
                if chunk:
                    got_data = True
                    buf += chunk
                    log += chunk
                    if len(log) > 8192:
                        del log[:-8192]

                    # scp's "ec2-user@1.2.3.4's password: " prompt has no
                    # trailing \r or \n — ssh just sits there waiting for
                    # input right after it — so the \r/\n line-splitting
                    # below would never see it; it'd sit in buf forever
                    # while we wait on a prompt nobody answers. Check for
                    # it directly against the raw buffer instead, and type
                    # the password back through the pty exactly like a
                    # person would (ssh disables local echo while reading
                    # it, so it never comes back through our own read()).
                    if not password_sent and self._PASSWORD_PROMPT_RE.search(buf[-64:]):
                        try:
                            os.write(read_fd, (self._password + "\n").encode())
                        except OSError:
                            pass
                        password_sent = True
                        buf = b""  # the prompt text itself has no % to parse

                    while b"\r" in buf or b"\n" in buf:
                        idx_r = buf.find(b"\r")
                        idx_n = buf.find(b"\n")
                        idx   = min(i for i in (idx_r, idx_n) if i != -1)
                        line, buf = buf[:idx], buf[idx + 1:]
                        pct = self._parse_pct(line)
                        if pct is not None and pct != last_pct:
                            last_pct = pct
                            done = int(self._total * pct / 100) if self._total else pct
                            self.progress.emit(done, self._total)

                    # `buf` is now just the current, unterminated line. The
                    # password prompt we answer was consumed above, so if
                    # what's left is a prompt (and not a progress line) it
                    # is one we can't answer: stop instead of hanging.
                    if buf and b"%" not in buf and self._OTHER_PROMPT_RE.search(buf):
                        prompt_abort = buf.decode(errors="replace").strip()
                        proc.kill()
                        break

            if not got_data and exit_code is not None:
                break

        if master_fd is not None:
            try:
                os.close(master_fd)
            except Exception:
                pass

        if self._cancelled:
            return

        ret = proc.wait()
        if prompt_abort:
            self.finished_err.emit(
                "scp stopped at a prompt it can't answer: {!r}".format(prompt_abort)
            )
            return
        if ret != 0:
            self.finished_err.emit(self._summarize_output(bytes(log), ret))
            return

        if self._total:
            self.progress.emit(self._total, self._total)
        self.finished_ok.emit()
