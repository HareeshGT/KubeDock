"""Deterministic tests for RFC 6238 TOTP helpers."""
import base64

from webapp.otp_auth import check_totp, current_counter, totp

# RFC 6238 Appendix B HMAC-SHA1 key (standard test vector).
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode("ascii").rstrip("=")


def test_rfc6238_six_digit_vector_and_clock_boundary():
    assert totp(RFC_SECRET, 59) == "287082"
    assert current_counter(29.999) == 0
    assert current_counter(30.0) == 1
    assert totp(RFC_SECRET, 29.999) != totp(RFC_SECRET, 30.0)


def test_totp_is_constant_within_a_time_step_and_rotates_each_step():
    assert totp(RFC_SECRET, 30) == totp(RFC_SECRET, 59)
    assert totp(RFC_SECRET, 60) == totp(RFC_SECRET, 89)
    assert current_counter(60) == current_counter(89)


def test_current_code_is_accepted_once_and_replay_is_rejected():
    code = totp(RFC_SECRET, 120)
    assert check_totp(RFC_SECRET, code, 120, None) == (True, "ok", 4)
    assert check_totp(RFC_SECRET, code, 120, 4) == (False, "replayed", 4)


def test_previous_step_is_expired_or_reported_as_replayed():
    previous = totp(RFC_SECRET, 90)
    assert check_totp(RFC_SECRET, previous, 120, None) == (False, "expired", 3)
    assert check_totp(RFC_SECRET, previous, 120, 3) == (False, "replayed", 3)


def test_malformed_and_wrong_codes_are_rejected():
    assert check_totp(RFC_SECRET, "abc123", 120, None)[0] is False
    assert check_totp(RFC_SECRET, "123", 120, None) == (False, "invalid", None)
    current = totp(RFC_SECRET, 120)
    wrong = str((int(current) + 1) % 1000000).zfill(6)
    assert check_totp(RFC_SECRET, wrong, 120, None) == (False, "invalid", None)
