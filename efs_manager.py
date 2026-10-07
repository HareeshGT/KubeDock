"""AWS EFS discovery and local NFS mount lifecycle for KubeDock.

Credentials are intentionally resolved through boto3's standard credential
chain. KubeDock does not store AWS access keys or secret keys.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Optional

try:
    import boto3
except ImportError:
    boto3 = None


class EFSException(RuntimeError):
    pass


@dataclass
class EFSFileSystem:
    filesystem_id: str
    name: str
    state: str
    size_bytes: int
    encrypted: bool
    creation_time: object


class EFSManager:
    """Discover and mount one EFS filesystem using the current AWS identity."""

    def __init__(self, region: Optional[str] = None, profile: Optional[str] = None):
        if boto3 is None:
            raise EFSException("boto3 is required for AWS EFS support.")
        self.region = region or None
        self.profile = profile.strip() if profile else None
        self.session = None
        self.client = None
        self.mount_path = None
        self.filesystem_id = None
        self.mount_target_ip = None

    def connect_aws(self):
        kwargs = {}
        if self.profile:
            kwargs["profile_name"] = self.profile
        if self.region:
            kwargs["region_name"] = self.region
        self.session = boto3.Session(**kwargs)
        region = self.session.region_name
        if not region:
            raise EFSException(
                "AWS region is not configured. Set AWS_DEFAULT_REGION/AWS_REGION "
                "or choose a region in the EFS dialog."
            )
        self.region = region
        self.client = self.session.client("efs", region_name=region)
        return self.session

    def caller_identity(self):
        if not self.session:
            self.connect_aws()
        sts = self.session.client("sts", region_name=self.region)
        return sts.get_caller_identity()

    def list_filesystems(self):
        if not self.client:
            self.connect_aws()
        paginator = self.client.get_paginator("describe_file_systems")
        result = []
        for page in paginator.paginate():
            for item in page.get("FileSystems", []):
                result.append(EFSFileSystem(
                    filesystem_id=item["FileSystemId"],
                    name=item.get("Name") or item["FileSystemId"],
                    state=item.get("LifeCycleState", "unknown"),
                    size_bytes=int(item.get("SizeInBytes", {}).get("Value", 0) or 0),
                    encrypted=bool(item.get("Encrypted", False)),
                    creation_time=item.get("CreationTime"),
                ))
        return result

    def list_regions(self):
        if not self.session:
            self.connect_aws()
        ec2 = self.session.client("ec2", region_name=self.region)
        return sorted(r["RegionName"] for r in ec2.describe_regions(
            AllRegions=False
        ).get("Regions", []))

    def mount_targets(self, filesystem_id):
        if not self.client:
            self.connect_aws()
        paginator = self.client.get_paginator("describe_mount_targets")
        targets = []
        for page in paginator.paginate(FileSystemId=filesystem_id):
            targets.extend(page.get("MountTargets", []))
        return targets

    def _select_mount_target(self, filesystem_id):
        targets = self.mount_targets(filesystem_id)
        available = [t for t in targets if t.get("LifeCycleState") == "available"]
        if not available:
            raise EFSException(
                "No available EFS mount target was found for {} in region {}."
                .format(filesystem_id, self.region)
            )
        # Prefer the first available target. NFS connectivity ultimately
        # determines whether the target is reachable from this machine.
        return available[0]

    @staticmethod
    def _run(cmd, timeout=30):
        try:
            return subprocess.run(
                cmd, check=False, capture_output=True, text=True, timeout=timeout
            )
        except FileNotFoundError:
            raise EFSException(
                "Required system command '{}' was not found.".format(cmd[0])
            )
        except subprocess.TimeoutExpired:
            raise EFSException("Command timed out: {}".format(" ".join(cmd)))

    @staticmethod
    def _is_mounted(path):
        if platform.system() == "Darwin":
            result = EFSManager._run(["mount"])
            return result.returncode == 0 and any(
                " on {} ".format(path) in line or line.rstrip().endswith(" on {}".format(path))
                for line in result.stdout.splitlines()
            )
        result = EFSManager._run(["mountpoint", "-q", path])
        return result.returncode == 0

    def mount(self, filesystem_id, mount_path=None):
        if not self.client:
            self.connect_aws()
        if self.mount_path:
            raise EFSException("An EFS filesystem is already mounted by this connection.")

        target = self._select_mount_target(filesystem_id)
        ip = target.get("IpAddress")
        if not ip:
            raise EFSException("The selected EFS mount target has no IP address.")

        if mount_path:
            mount_path = os.path.abspath(os.path.expanduser(mount_path))
            os.makedirs(mount_path, exist_ok=True)
        else:
            mount_path = tempfile.mkdtemp(prefix="kubedock-efs-")

        if self._is_mounted(mount_path):
            raise EFSException("Mount path is already in use: {}".format(mount_path))

        system = platform.system()
        if system == "Darwin":
            # macOS ships /sbin/mount_nfs. EFS exposes NFSv4.1.
            source = "{}:/".format(ip)
            cmd = [
                "mount_nfs",
                "-o", "vers=4,proto=tcp,port=2049",
                source, mount_path,
            ]
        elif system == "Linux":
            source = "{}:/".format(ip)
            cmd = [
                "mount", "-t", "nfs4",
                "-o", "nfsvers=4.1,proto=tcp,port=2049",
                source, mount_path,
            ]
        else:
            raise EFSException(
                "EFS NFS mounting is not implemented for {}.".format(system)
            )

        result = self._run(cmd, timeout=45)
        if result.returncode != 0:
            shutil.rmtree(mount_path, ignore_errors=True)
            detail = (result.stderr or result.stdout).strip()
            raise EFSException(
                "Could not mount EFS {} via {}. "
                "Make sure this machine can reach the EFS mount target on TCP/2049.{}"
                .format(filesystem_id, ip, " " + detail if detail else "")
            )

        self.mount_path = mount_path
        self.filesystem_id = filesystem_id
        self.mount_target_ip = ip
        return mount_path

    def unmount(self):
        path = self.mount_path
        if not path:
            return
        system = platform.system()
        cmd = ["umount", path] if system != "Darwin" else ["umount", path]
        result = self._run(cmd, timeout=30)
        if result.returncode != 0 and self._is_mounted(path):
            # macOS sometimes needs diskutil's unmount path for a busy NFS mount.
            if system == "Darwin":
                self._run(["diskutil", "unmount", path], timeout=30)
        if self._is_mounted(path):
            raise EFSException(
                "EFS mount is still busy: {}. Close files using it and try again."
                .format(path)
            )
        try:
            os.rmdir(path)
        except OSError:
            pass
        self.mount_path = None
        self.filesystem_id = None
        self.mount_target_ip = None

    def close(self):
        try:
            self.unmount()
        finally:
            self.client = None
            self.session = None
