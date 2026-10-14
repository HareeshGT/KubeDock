"""sudo_fs.py — SudoFS SFTP wrapper that can transparently sudo all operations."""

import io
import os
import posixpath
import shlex
import stat as _stat
from typing import Optional, Tuple


def _perms_to_mode(perms: str) -> int:
    """Convert an ls-style permission string (e.g. '-rwxr-xr-x') to a mode int."""
    mode  = 0
    ftype = perms[0]
    if ftype == 'd':   mode |= _stat.S_IFDIR
    elif ftype == 'l': mode |= _stat.S_IFLNK
    else:              mode |= _stat.S_IFREG

    mapping = [
        (1, _stat.S_IRUSR), (2, _stat.S_IWUSR), (3, _stat.S_IXUSR),
        (4, _stat.S_IRGRP), (5, _stat.S_IWGRP), (6, _stat.S_IXGRP),
        (7, _stat.S_IROTH), (8, _stat.S_IWOTH), (9, _stat.S_IXOTH),
    ]
    for idx, bit in mapping:
        if idx < len(perms) and perms[idx] not in ('-', 'T', 'S'):
            mode |= bit
    return mode


class SudoFS:
    """
    Thin wrapper around paramiko SFTP that can re-route every operation through
    ``sudo -u <username>`` shell commands when a sudo user is active.
    """

    def __init__(self, sftp, ssh):
        self._sftp = sftp
        self._ssh  = ssh
        self.sudo_user    = None
        self._sudo_prefix = ""

    # ── Configuration ─────────────────────────────────────────
    def set_sudo_user(self, username):
        # type: (Optional[str]) -> None
        self.sudo_user    = username
        self._sudo_prefix = "sudo -n -u {} ".format(shlex.quote(username)) if username else ""

    # ── Internal helpers ──────────────────────────────────────
    def _run(self, cmd, timeout=60):
        # type: (str, int) -> Tuple[int, str, str]
        # Goes through the managed/pooled channel path like every other
        # remote command. The previous raw ``ssh.exec_command`` ran on the
        # primary transport outside the channel limiter, had no timeout (a
        # stalled server blocked the caller forever), and waited for the exit
        # status *before* draining stdout — which can deadlock once the
        # output outgrows the channel window. Output is now drained first.
        from workers import managed_exec_command
        with managed_exec_command(self._ssh, cmd) as (_stdin, out, err):
            out.channel.settimeout(timeout)
            stdout = out.read()
            stderr = err.read()
            code = out.channel.recv_exit_status()
        return code, stdout.decode(errors="replace"), stderr.decode(errors="replace")

    def _sq(self, path):
        # type: (str) -> str
        """Single-quote a path safely for shell injection."""
        return "'" + path.replace("'", "'\\''") + "'"

    def _run_or_raise(self, cmd):
        code, out, err = self._run(cmd)
        if code != 0:
            raise PermissionError((err or out or "remote operation failed").strip())
    # ── Path resolution ───────────────────────────────────────
    def normalize(self, path):
        # type: (str) -> str
        if not self.sudo_user or path in ("", "~"):
            if self.sudo_user and path in ("", "~"):
                code, out, err = self._run("sudo -n -u {} sh -c 'printf \"%s\n\" \"$HOME\"'".format(shlex.quote(self.sudo_user)))
                return out.strip() or "/home/{}".format(self.sudo_user)
            return self._sftp.normalize(path or ".")
        code, out, _ = self._run(
            "{prefix}realpath {p} 2>/dev/null "
            "|| echo {p}".format(prefix=self._sudo_prefix, p=self._sq(path))
        )
        return out.strip() or path

    # ── Directory listing ─────────────────────────────────────
    def listdir_attr(self, path):
        # type: (str) -> list
        if not self.sudo_user:
            return self._sftp.listdir_attr(path)

        code, out, err = self._run("{prefix}ls -la {p}".format(
            prefix=self._sudo_prefix, p=self._sq(path)))
        if code != 0:
            raise PermissionError((err or out or "directory listing failed").strip())

        entries = []
        for line in out.splitlines():
            if line.startswith("total") or not line.strip():
                continue
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            perms_str, _, _, _, size_str, _, _, _, name = parts
            if name in (".", ".."):
                continue
            mode = _perms_to_mode(perms_str)
            try:
                size = int(size_str)
            except ValueError:
                size = 0

            class _Attr:
                pass

            a          = _Attr()
            a.filename = name.split(" -> ")[0]
            a.st_mode  = mode
            a.st_size  = size
            entries.append(a)
        return entries

    def listdir(self, path):
        # type: (str) -> list
        return [e.filename for e in self.listdir_attr(path)]

    # ── File stat ─────────────────────────────────────────────
    def stat(self, path):
        # type: (str) -> object
        if not self.sudo_user:
            return self._sftp.stat(path)

        code, out, err = self._run("{prefix}stat -c '%f %s' {p}".format(
            prefix=self._sudo_prefix, p=self._sq(path)))
        if code != 0:
            raise FileNotFoundError((err or out or "stat failed").strip())
        parts = out.strip().split()
        if len(parts) < 2:
            raise FileNotFoundError("stat failed for {}".format(path))

        class _St:
            pass

        s         = _St()
        s.st_mode = int(parts[0], 16)
        s.st_size = int(parts[1])
        return s

    # ── File open / read ──────────────────────────────────────
    def open(self, path, mode="r"):
        # type: (str, str) -> object
        if not self.sudo_user:
            return self._sftp.open(path, mode)
        if any(flag in mode for flag in ("w", "a", "+")):
            raise NotImplementedError(
                "Switched-user writes must use SudoFS.write_bytes() so bytes and permissions are preserved."
            )
        code, out, err = self._run("{prefix}cat {p}".format(
            prefix=self._sudo_prefix, p=self._sq(path)))
        if code != 0:
            raise PermissionError((err or out or "remote read failed").strip())
        return io.BytesIO(out.encode("utf-8", errors="replace"))

    @staticmethod
    def _atomic_write_script(remote_path, expected_size, temp_dir=None, owner=None):
        """Build a POSIX shell script that atomically replaces one remote file.

        The temporary file is created with mktemp (mode 0600) and lives beside
        the destination when writing as the target user, so the final rename is
        atomic on the same filesystem. The size check prevents a short/failed
        stdin transfer from replacing a valid destination with partial content.
        """
        expected_size = int(expected_size)
        if temp_dir:
            template = posixpath.join(temp_dir, ".kubedock-save.XXXXXX")
        else:
            parent = posixpath.dirname(remote_path) or "."
            template = posixpath.join(parent, ".kubedock-save.XXXXXX")

        script = [
            "umask 077",
            "tmp=$(mktemp {}) || exit $?".format(shlex.quote(template)),
            """trap 'rm -f "$tmp"' 0 HUP INT TERM""",
            'cat > "$tmp" || exit $?',
            'actual=$(wc -c < "$tmp" | tr -d \'[:space:]\') || exit $?',
            '[ "$actual" -eq {} ] || {{ echo "incomplete file write" >&2; exit 1; }}'.format(expected_size),
            'if [ -e {dest} ]; then mode=$(stat -c \'%a\' {dest} 2>/dev/null) || mode=$(stat -f \'%Lp\' {dest} 2>/dev/null) || exit 1; else mode=0644; fi'.format(dest=shlex.quote(remote_path)),
            'chmod "$mode" "$tmp" || exit $?'
        ]
        if owner:
            script.append('chown -- {} "$tmp" || exit $?'.format(shlex.quote(owner)))
        script.append('mv -f -- "$tmp" {}'.format(shlex.quote(remote_path)))
        return "; ".join(script)

    @staticmethod
    def _permission_failure(detail):
        low = (detail or "").lower()
        return any(marker in low for marker in (
            "permission denied", "operation not permitted", "not permitted",
            "access denied",
        ))

    def _execute_with_input(self, command, source):
        """Stream a binary file-like object to a remote command's stdin."""
        from workers import managed_exec_command

        write_error = None
        with managed_exec_command(self._ssh, command) as (stdin, stdout, stderr):
            channel = stdout.channel
            channel.settimeout(60)
            try:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    stdin.write(chunk)
                stdin.flush()
            except Exception as exc:
                # Read the command's exit status and diagnostic below. A
                # command that rejected its permissions early may close stdin;
                # that should still allow the controlled fallback decision.
                write_error = exc
            finally:
                try:
                    channel.shutdown_write()
                except Exception:
                    pass

            out = stdout.read()
            err = stderr.read()
            code = channel.recv_exit_status()

        if write_error is not None and code == 0:
            raise IOError("The remote command completed after a local stream-write error: {}".format(write_error))
        return code, out.decode(errors="replace"), err.decode(errors="replace")

    def _write_stream(self, remote_path, source, expected_size):
        """Write as the selected user first; keep root fallback for protected paths."""
        if not self.sudo_user:
            with self._sftp.open(remote_path, "wb") as remote:
                while True:
                    chunk = source.read(64 * 1024)
                    if not chunk:
                        break
                    remote.write(chunk)
            return

        target_script = self._atomic_write_script(remote_path, expected_size)
        target_cmd = "sudo -n -u {} sh -c {}".format(
            shlex.quote(self.sudo_user), shlex.quote(target_script)
        )
        code, out, err = self._execute_with_input(target_cmd, source)
        if code == 0:
            return

        target_detail = (err or out or "exit status {}".format(code)).strip()
        if not self._permission_failure(target_detail):
            raise PermissionError(
                "Could not save '{}' as '{}': {}".format(
                    remote_path, self.sudo_user, target_detail
                )
            )

        # Only a clear filesystem permission failure triggers the legacy
        # privileged path. No retry for bad paths, unavailable commands,
        # interrupted transfers, or sudo/authentication failures.
        source.seek(0)
        root_script = self._atomic_write_script(
            remote_path, expected_size, temp_dir="/tmp", owner=self.sudo_user
        )
        root_cmd = "sudo -n sh -c {}".format(shlex.quote(root_script))
        code, out, err = self._execute_with_input(root_cmd, source)
        if code != 0:
            root_detail = (err or out or "exit status {}".format(code)).strip()
            raise PermissionError(
                "Could not save '{}' as '{}': {}. The privileged fallback also failed: {}".format(
                    remote_path, self.sudo_user, target_detail, root_detail
                )
            )

    def write_bytes(self, remote_path, data):
        """Write bytes through SFTP normally, or under the switched SSH user."""
        payload = bytes(data)
        self._write_stream(remote_path, io.BytesIO(payload), len(payload))

    # ── Download / upload ─────────────────────────────────────
    def get(self, remote_path, local_path):
        # type: (str, str) -> None
        if not self.sudo_user:
            return self._sftp.get(remote_path, local_path)
        code, out, err = self._run("{prefix}cat {p}".format(
            prefix=self._sudo_prefix, p=self._sq(remote_path)))
        if code != 0:
            raise PermissionError((err or out or "download failed").strip())
        with open(local_path, "wb") as f:
            f.write(out.encode("utf-8", errors="replace"))

    def put(self, local_path, remote_path):
        # type: (str, str) -> None
        if not self.sudo_user:
            return self._sftp.put(local_path, remote_path)

        size = os.path.getsize(local_path)
        with open(local_path, "rb") as source:
            return self._write_stream(remote_path, source, size)

    # ── Directory operations ──────────────────────────────────
    def mkdir(self, path):
        # type: (str) -> None
        if not self.sudo_user:
            return self._sftp.mkdir(path)
        self._run_or_raise("{prefix}mkdir {p} 2>&1".format(
            prefix=self._sudo_prefix, p=self._sq(path)))

    def rmdir(self, path):
        # type: (str) -> None
        if not self.sudo_user:
            return self._sftp.rmdir(path)
        self._run_or_raise("{prefix}rmdir {p} 2>&1".format(
            prefix=self._sudo_prefix, p=self._sq(path)))

    # ── File removal ──────────────────────────────────────────
    def remove(self, path):
        # type: (str) -> None
        if not self.sudo_user:
            return self._sftp.remove(path)
        self._run_or_raise("{prefix}rm {p} 2>&1".format(
            prefix=self._sudo_prefix, p=self._sq(path)))

    # ── Rename / move ─────────────────────────────────────────
    def rename(self, old_path, new_path):
        # type: (str, str) -> None
        if not self.sudo_user:
            return self._sftp.rename(old_path, new_path)
        self._run_or_raise("{prefix}mv -n {old} {new} 2>&1".format(
            prefix=self._sudo_prefix, old=self._sq(old_path), new=self._sq(new_path)))

    # ── Teardown ──────────────────────────────────────────────
    def close(self):
        self._sftp.close()