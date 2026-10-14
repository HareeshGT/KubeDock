"""Regression tests for switched-user remote writes without a live SSH host."""

import pytest

from sudo_fs import SudoFS


class RecordingSFTP:
    def __init__(self):
        self.path = None
        self.mode = None
        self.data = bytearray()

    def open(self, path, mode):
        self.path, self.mode = path, mode
        outer = self

        class Handle:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def write(self, chunk):
                outer.data.extend(chunk)
                return len(chunk)

        return Handle()


def make_fs():
    return SudoFS(RecordingSFTP(), ssh=object())


def test_write_bytes_without_switched_user_preserves_binary_payload():
    fs = make_fs()
    payload = b"\x00binary\xffpayload"

    fs.write_bytes("/tmp/file.bin", payload)

    assert fs._sftp.path == "/tmp/file.bin"
    assert fs._sftp.mode == "wb"
    assert bytes(fs._sftp.data) == payload


def test_atomic_write_script_quotes_destination_and_checks_size():
    script = SudoFS._atomic_write_script("/etc/my service.conf", 42)

    assert "mktemp" in script
    assert "'/etc/my service.conf'" in script
    assert 'cat > "$tmp"' in script
    assert "wc -c" in script
    assert '[ "$actual" -eq 42 ]' in script
    assert 'chmod "$mode" "$tmp"' in script
    assert script.index('chmod "$mode" "$tmp"') < script.index('mv -f "$tmp"')


def test_switched_user_success_does_not_escalate_to_root():
    fs = make_fs()
    fs.set_sudo_user("alice")
    payload = b"target-owned bytes"
    calls = []

    def fake_execute(command, source):
        calls.append((command, source.read()))
        return 0, "", ""

    fs._execute_with_input = fake_execute
    fs.write_bytes("/home/alice/example.txt", payload)

    assert len(calls) == 1
    assert calls[0][0].startswith("sudo -n -u alice sh -c ")
    assert calls[0][1] == payload


def test_permission_denied_retries_with_scoped_root_fallback():
    fs = make_fs()
    fs.set_sudo_user("alice")
    payload = b"\x00binary\xffpayload"
    calls = []

    def fake_execute(command, source):
        calls.append((command, source.read()))
        if len(calls) == 1:
            return 1, "", "Permission denied"
        return 0, "", ""

    fs._execute_with_input = fake_execute
    fs.write_bytes("/etc/protected.bin", payload)

    assert len(calls) == 2
    assert calls[0][0].startswith("sudo -n -u alice sh -c ")
    assert calls[1][0].startswith("sudo -n sh -c ")
    assert "chown alice \"$tmp\"" in calls[1][0]
    assert calls[0][1] == calls[1][1] == payload


def test_sudo_authentication_failure_does_not_trigger_root_retry():
    fs = make_fs()
    fs.set_sudo_user("alice")
    calls = []

    def fake_execute(command, source):
        calls.append(command)
        source.read()
        return 1, "", "sudo: a password is required"

    fs._execute_with_input = fake_execute
    with pytest.raises(PermissionError, match="password is required"):
        fs.write_bytes("/etc/file.conf", b"content")

    assert len(calls) == 1
    assert calls[0].startswith("sudo -n -u alice sh -c ")
