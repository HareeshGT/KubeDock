"""sudo_fs.py — SudoFS SFTP wrapper that can transparently sudo all operations."""

import os
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
        self._sudo_prefix = "sudo -u {} ".format(self._sq(username)) if username else ""

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
                code, out, _ = self._run("sudo -u {} sh -c 'echo $HOME'".format(self._sq(self.sudo_user)))
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
        import io
        code, out, _ = self._run("{prefix}cat {p} 2>/dev/null".format(
            prefix=self._sudo_prefix, p=self._sq(path)))
        return io.BytesIO(out.encode("utf-8", errors="replace"))

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
        tmp = "/tmp/.ec2mgr_upload_{}".format(os.getpid())
        self._sftp.put(local_path, tmp)
        self._run_or_raise(
            "sudo mv {tmp} {dst} && sudo chown {user} {dst}".format(
                tmp=self._sq(tmp), dst=self._sq(remote_path), user=self._sq(self.sudo_user))
        )

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