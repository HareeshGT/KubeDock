"""Regression tests for switched-user remote writes.

These tests exercise shell construction and write routing without requiring a
live SSH server or sudo privileges. Remote integration cases still need to be
run against Linux and macOS hosts.
"""

import pytest

from sudo_fs import SudoFS


class _RecordingSFTP:
    def __init__(self):
        self.path = None
        self.mode = None
        self.data = bytearray()

    def open(self, path, mode):
        self.path, self.mode = path, mode
        outer = self

        class _Handle:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return None

            def write(self, chunk):
                outer.data.extend(chunk)
                return len(chunk)

        return _Handle()


def _fs():
    return SudoFS(_RecordingSFTP(), ssh=object())


def test_unselected_user_write_uses_plain_binary_sftp():
    fs = _fs()
    payload = b"\x00binary\xffdata"

    fs.write_bytes("/tmp/example.bin", payload)

    assert fs._sftp.path == "/tmp/example.bin"
    assert fs._sftp.mode == "wb"
    assert bytes(fs._sftp.data) == payload


def test_atomic_script_quotes_paths_checks_full_size_and_preserves_mode():
    script = SudoFS._atomic_write_script("/etc/my service.conf", 42)

    assert "mktemp /etc/.kubedock-save.XXXXXX" in script
    assert "cat > \"$tmp\"" in script
    assert "wc -c" in script
    assert "[ \"$actual\" -eq 42 ]" in script
    assert "stat -c '%a' '/etc/my service.conf'" in script
    assert "stat -f '%Lp' '/etc/my service.conf'" in script
    assert 'chmod "$mode" "$tmp"' in script
    assert script.index('chmod "$mode" "$tmp"') < script.index("mv -f -- \"$tmp\"")


def test_switched_user_write_tries_target_first_and_rewinds_before_root_fallback():
    fs = _fs()
    fs.set_sudo_user("alice")
    payload = b"\x00binary\xffdata"
    attempts = []

    def fake_execute(command, source):
        attempts.append((command, source.read()))
        if len(attempts) == 1:
            return 1, "", "Permission denied"
        return 0, "", ""

    fs._execute_with_input = fake_execute
    fs.write_bytes("/etc/example.bin", payload)

    assert len(attempts) == 2
    assert attempts[0][0].startswith("sudo -n -u alice sh -c ")
    assert attempts[1][0].startswith("sudo -n sh -c ")
    assert "chown -- alice \"$tmp\"" in attempts[1][0]
    assert attempts[0][1] == attempts[1][1] == payload


def test_switched_user_write_does_not_fallback_for_sudo_authentication_failure():
    fs = _fs()
    fs.set_sudo_user("alice")
    attempts = []

    def fake_execute(command, source):
        attempts.append(command)
        source.read()
        return 1, "", "sudo: a password is required"

    fs._execute_with_input = fake_execute

    with pytest.raises(PermissionError, match="password is required"):
        fs.write_bytes("/etc/example.conf", b"new value")

    assert len(attempts) == 1
    assert attempts[0].startswith("sudo -n -u alice sh -c ")


def test_atomic_root_fallback_chowns_temporary_file_before_replace():
    script = SudoFS._atomic_write_script(
        "/etc/example.conf", 12, temp_dir="/tmp", owner="alice"
    )

    assert "mktemp /tmp/.kubedock-save.XXXXXX" in script
    assert script.index('chown -- alice "$tmp"') < script.index('mv -f -- "$tmp"')
