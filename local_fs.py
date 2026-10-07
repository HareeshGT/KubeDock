"""Local filesystem adapter used for mounted AWS EFS volumes.

It exposes the small SFTP-like surface KubeDock's file manager already uses,
while keeping every path confined to the mounted filesystem.
"""
from __future__ import annotations

import os
import shutil
import stat


class _LocalFile:
    def __init__(self, fileobj):
        self._file = fileobj
        self.MAX_REQUEST_SIZE = 256 * 1024

    def prefetch(self, *_args, **_kwargs):
        return None

    def set_pipelined(self, *_args, **_kwargs):
        return None

    def read(self, *args):
        return self._file.read(*args)

    def write(self, *args):
        return self._file.write(*args)

    def seek(self, *args):
        return self._file.seek(*args)

    def tell(self):
        return self._file.tell()

    def flush(self):
        return self._file.flush()

    def close(self):
        return self._file.close()

    def __enter__(self):
        self._file.__enter__()
        return self

    def __exit__(self, *exc):
        return self._file.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._file, name)


class LocalFS:
    """SFTP-compatible adapter rooted at an already-mounted local path."""

    is_local_fs = True

    def __init__(self, root):
        self.root = os.path.realpath(os.path.abspath(root))
        if not os.path.isdir(self.root):
            raise ValueError("EFS mount path is not a directory: {}".format(root))
        self.sudo_user = None
        self._sftp = self

    def _resolve(self, path):
        path = path or "/"
        if path in ("/", "~"):
            return self.root
        if os.path.isabs(path):
            # KubeDock stores absolute paths for local/mounted filesystems.
            candidate = os.path.realpath(path)
        else:
            candidate = os.path.realpath(os.path.join(self.root, path))
        if candidate != self.root and not candidate.startswith(self.root + os.sep):
            raise PermissionError("Path escapes the mounted EFS filesystem.")
        return candidate

    def normalize(self, path):
        return self._resolve(path)

    def listdir_attr(self, path):
        result = []
        for entry in os.scandir(self._resolve(path)):
            st = entry.stat(follow_symlinks=False)
            result.append(type("LocalAttr", (), {
                "filename": entry.name,
                "st_mode": st.st_mode,
                "st_size": st.st_size,
            })())
        return result

    def listdir(self, path):
        return [x.filename for x in self.listdir_attr(path)]

    def stat(self, path):
        return os.stat(self._resolve(path))

    def open(self, path, mode="r"):
        binary_mode = mode if "b" in mode else mode + "b"
        return _LocalFile(open(self._resolve(path), binary_mode))

    def mkdir(self, path):
        os.mkdir(self._resolve(path))

    def rmdir(self, path):
        os.rmdir(self._resolve(path))

    def remove(self, path):
        os.remove(self._resolve(path))

    def rename(self, old, new):
        os.rename(self._resolve(old), self._resolve(new))

    def get(self, remote, local, callback=None):
        source = self._resolve(remote)
        total = os.path.getsize(source)
        done = 0
        with open(source, "rb") as src, open(local, "wb") as dst:
            while True:
                data = src.read(256 * 1024)
                if not data:
                    break
                dst.write(data)
                done += len(data)
                if callback:
                    callback(len(data), done)

    def put(self, local, remote, callback=None):
        destination = self._resolve(remote)
        total = os.path.getsize(local)
        done = 0
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(local, "rb") as src, open(destination, "wb") as dst:
            while True:
                data = src.read(256 * 1024)
                if not data:
                    break
                dst.write(data)
                done += len(data)
                if callback:
                    callback(len(data), done)

    def close(self):
        # The EFSManager owns the actual NFS mount lifecycle.
        pass
