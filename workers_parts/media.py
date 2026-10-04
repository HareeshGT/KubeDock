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

class _ChannelReader:
    """Minimal file-like wrapper around a raw paramiko Channel, for the
    sudo-cat streaming fallback (sequential-only, no seek)."""

    def __init__(self, channel):
        self._channel = channel
        self._channel.settimeout(0.5)
        self._eof = False

    def read(self, n):
        if self._eof:
            return b""
        while True:
            try:
                if self._channel.recv_ready():
                    buf = self._channel.recv(n)
                    if not buf:
                        self._eof = True
                    return buf
            except Exception:
                pass
            if self._channel.exit_status_ready() and not self._channel.recv_ready():
                self._eof = True
                return b""

    def close(self):
        close_managed_session(self._channel)
        self._channel = None


class _SFTPStreamReader:
    """Sequential reader over one file opened on its *own* SFTP channel.

    paramiko's SFTPClient is not safe for several threads reading at once
    (interleaved packet reads surface as "Garbage packet received"), and a
    player routinely has more than one request in flight (a probe, the real
    read, a seek). So every HTTP request gets a dedicated SFTP channel on
    the shared SSH transport instead of sharing a single client.

    Reads are pipelined with paramiko's prefetch to hide network latency,
    but only one WINDOW at a time and never past the end of the requested
    range. That keeps the number of queued requests small (a whole-file
    prefetch queues one per 32 KiB — tens of thousands for a big video —
    on every seek) and makes abandoning a reader cheap.

    Paramiko's default 32 KiB request size is deliberate: servers such as
    OpenSSH's sftp-server cap reads below larger sizes, which silently
    breaks prefetch pipelining and makes playback crawl.
    """

    WINDOW = 16 * 1024 * 1024

    def __init__(self, ssh, remote_path, start, stop):
        """Read bytes [start, stop) of ``remote_path``."""
        self._client = ssh.open_sftp()
        self._f = None
        self._pos = start
        self._stop = stop
        self._window_end = start      # nothing prefetched yet
        try:
            self._f = self._client.open(remote_path, "rb")
        except Exception:
            self.close()
            raise

    def read(self, n):
        if self._pos >= self._stop:
            return b""
        if self._pos >= self._window_end:
            self._window_end = min(self._pos + self.WINDOW, self._stop)
            self._f.seek(self._pos)
            try:
                self._f.prefetch(self._window_end)
            except Exception:
                pass    # falls back to plain (slower) synchronous reads
        data = self._f.read(min(n, self._window_end - self._pos))
        self._pos += len(data)
        return data

    def close(self):
        for closer in (getattr(self._f, "close", None), self._client.close):
            if closer is None:
                continue
            try:
                closer()
            except Exception:
                pass


class _RangeHTTPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        pass  # a request per byte-range chunk would otherwise spam stderr

    def do_HEAD(self):
        self.close_connection = True
        self._send_headers(0, max(self.server.file_size - 1, 0), ranged=False)

    def do_GET(self):
        self.close_connection = True
        size  = self.server.file_size
        start, end = 0, max(size - 1, 0)
        ranged = False
        rng = self.headers.get("Range")
        if rng and self.server.supports_range and size:
            m = re.match(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)", rng)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    end   = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
                else:
                    # suffix range "bytes=-N": the last N bytes
                    start = max(size - int(m.group(2)), 0)
                    end   = size - 1
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */{}".format(size))
                    self.send_header("Content-Length", "0")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    return
                ranged = True
        # Open the remote reader *before* committing to a 200/206 so a
        # failure to reach the file is reported as an error instead of a
        # silently truncated body.
        try:
            reader = self.server.open_reader(start, end)
        except Exception:
            self.send_error(502, "Remote read failed")
            return
        self.server.track_reader(reader)
        try:
            self._send_headers(start, end, ranged)
            self._stream_range(reader, start, end)
        finally:
            self.server.untrack_reader(reader)
            try:
                reader.close()
            except Exception:
                pass

    def _send_headers(self, start, end, ranged):
        size   = self.server.file_size
        length = (end - start + 1) if size else None
        if ranged:
            self.send_response(206)
            self.send_header("Content-Range", "bytes {}-{}/{}".format(start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", self.server.content_type)
        self.send_header("Accept-Ranges", "bytes" if self.server.supports_range else "none")
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Connection", "close")
        self.end_headers()

    def _stream_range(self, reader, start, end):
        size      = self.server.file_size
        remaining = (end - start + 1) if size else None
        chunk     = 65536
        try:
            while remaining is None or remaining > 0:
                want = chunk if remaining is None else min(chunk, remaining)
                buf = reader.read(want)
                if not buf:
                    break
                self.wfile.write(buf)
                if remaining is not None:
                    remaining -= len(buf)
        except Exception:
            # Player went away (seek / close) or the remote read failed
            # mid-stream: either way this response is over.
            pass


class _MediaStreamHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._readers = set()
        self._readers_lock = threading.Lock()

    def track_reader(self, reader):
        with self._readers_lock:
            self._readers.add(reader)

    def untrack_reader(self, reader):
        with self._readers_lock:
            self._readers.discard(reader)

    def close_all_readers(self):
        """Abort every in-flight remote read (used when the player closes)."""
        with self._readers_lock:
            readers = list(self._readers)
            self._readers.clear()
        for r in readers:
            try:
                r.close()
            except Exception:
                pass


class MediaStreamServer:
    """Serves exactly one remote file over a loopback-only local HTTP
    server so QMediaPlayer can stream it directly — with real Range-based
    seeking — instead of the app downloading (or converting) the file on
    local disk first.

    - SSH/SFTP, no sudo (or the login account can read the file): opens a
      dedicated SFTP channel and serves byte-range reads straight from it,
      with a bounded read-ahead window.
    - SSH with a sudo user active and the file not reachable over plain
      SFTP: streams ``sudo -u <user> cat`` (or ``tail -c +N`` to start at
      an offset, so seeking works) over a raw channel.
    - FTP/FTPS: each request opens its own connection and uses
      ``REST <offset>`` + ``RETR``; seeking works if the server supports
      REST.
    """

    def __init__(self, ssh, sftp, remote_path, sudo_user=None):
        self._ssh        = ssh
        self._sftp       = sftp     # SudoFS or FTPFS instance
        self._remote      = remote_path
        self._sudo_user   = sudo_user
        self._httpd       = None
        self._thread      = None
        self.url            = None
        self.supports_range = False

    def start(self) -> str:
        content_type = mimetypes.guess_type(self._remote)[0] or "application/octet-stream"

        is_ftp = hasattr(self._sftp, "_ftp")
        size, supports_range = 0, False
        if not is_ftp and not self._sudo_user:
            try:
                probe = self._ssh.open_sftp()
                try:
                    size = probe.stat(self._remote).st_size
                finally:
                    probe.close()
                supports_range = True
            except Exception:
                size = 0
                supports_range = False

        if is_ftp:
            fs = self._sftp
            remote = self._remote
            size, supports_range = fs.stream_probe(remote)

            def open_reader(start, end):
                return fs.open_stream(remote, start)
        elif supports_range:
            ssh = self._ssh
            remote = self._remote

            def open_reader(start, end):
                return _SFTPStreamReader(ssh, remote, start, end + 1)
        else:
            prefix = getattr(self._sftp, "_sudo_prefix", "")
            sq     = getattr(self._sftp, "_sq", lambda p: "'" + p.replace("'", "'\\''") + "'")
            target = sq(self._remote)
            ssh    = self._ssh

            def open_reader(start, end):
                channel = open_managed_session(ssh)
                if start:
                    # tail -c +N begins output at byte N (1-based)
                    cmd = "{}tail -c +{} {} 2>/dev/null".format(prefix, int(start) + 1, target)
                else:
                    cmd = "{}cat {} 2>/dev/null".format(prefix, target)
                channel.exec_command(cmd)
                return _ChannelReader(channel)

            try:
                size = self._sftp.stat(self._remote).st_size
            except Exception:
                size = 0
            # A known size is all it takes to honour byte ranges here.
            supports_range = size > 0

        httpd = _MediaStreamHTTPServer(("127.0.0.1", 0), _RangeHTTPRequestHandler)
        httpd.file_size      = size
        httpd.content_type   = content_type
        httpd.supports_range = supports_range
        httpd.open_reader    = open_reader
        httpd.timeout        = 30

        self._httpd  = httpd
        self.supports_range = supports_range
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()

        port = httpd.server_address[1]
        self.url = "http://127.0.0.1:{}/stream".format(port)
        return self.url

    def stop(self):
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd.close_all_readers()
            self._httpd = None


class VideoTranscodeWorker(QThread):
    """Fallback decoder for video containers/codecs unavailable to QtMultimedia.

    ffmpeg reads the existing loopback media stream and writes a temporary
    H.264/AAC MP4.  MP4/H.264/AAC is broadly supported by QtMultimedia on
    macOS, Windows and Linux, while MKV itself may contain codecs that the
    platform multimedia backend cannot decode.
    """

    ready = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, source_url: str, output_path: str, ffmpeg_path: str):
        super().__init__()
        self._source_url = source_url
        self._output_path = output_path
        self._ffmpeg_path = ffmpeg_path
        self._process = None
        self.finished.connect(self.deleteLater)

    def run(self):
        try:
            self._process = subprocess.Popen(
                [
                    self._ffmpeg_path, "-hide_banner", "-loglevel", "error",
                    "-y", "-i", self._source_url,
                    "-map", "0:v:0",
                    "-map", "0:a:0?",
                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "23",
                    "-pix_fmt", "yuv420p",
                    "-c:a", "aac",
                    "-b:a", "160k",
                    "-movflags", "+faststart",
                    self._output_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _, stderr = self._process.communicate()
            if self._process.returncode != 0:
                raise RuntimeError(
                    stderr.decode("utf-8", "replace").strip()
                    or "ffmpeg failed to decode the video"
                )
            if not os.path.isfile(self._output_path) or os.path.getsize(self._output_path) == 0:
                raise RuntimeError("ffmpeg produced an empty video")
            self.ready.emit(self._output_path)
        except Exception as e:
            try:
                if os.path.exists(self._output_path):
                    os.unlink(self._output_path)
            except OSError:
                pass
            self.error.emit(str(e))

    def stop(self):
        proc = self._process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class AudioTranscodeWorker(QThread):
    """Fallback decoder for audio codecs unavailable to QtMultimedia.

    ffmpeg reads the existing loopback media stream and produces a temporary
    PCM WAV that QtMultimedia can decode reliably.
    """

    ready = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, source_url: str, output_path: str, ffmpeg_path: str):
        super().__init__()
        self._source_url = source_url
        self._output_path = output_path
        self._ffmpeg_path = ffmpeg_path
        self._process = None
        self.finished.connect(self.deleteLater)

    def run(self):
        try:
            self._process = subprocess.Popen(
                [
                    self._ffmpeg_path, "-hide_banner", "-loglevel", "error",
                    "-y", "-i", self._source_url, "-vn",
                    "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2",
                    self._output_path,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _, stderr = self._process.communicate()
            if self._process.returncode != 0:
                raise RuntimeError(
                    stderr.decode("utf-8", "replace").strip()
                    or "ffmpeg failed to decode the audio"
                )
            if not os.path.isfile(self._output_path) or os.path.getsize(self._output_path) == 0:
                raise RuntimeError("ffmpeg produced an empty audio file")
            self.ready.emit(self._output_path)
        except Exception as e:
            try:
                if os.path.exists(self._output_path):
                    os.unlink(self._output_path)
            except OSError:
                pass
            self.error.emit(str(e))

    def stop(self):
        proc = self._process
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


class _StreamServerStartWorker(QThread):
    """Starts a MediaStreamServer off the UI thread (opening the dedicated
    SFTP channel it needs can take a moment on a slow link)."""

    ready = pyqtSignal(str)
    error = pyqtSignal(str)

    def __init__(self, server: "MediaStreamServer"):
        super().__init__()
        self._server = server
        self.finished.connect(self.deleteLater)

    def run(self):
        try:
            url = self._server.start()
            self.ready.emit(url)
        except Exception as e:
            self.error.emit(str(e))
