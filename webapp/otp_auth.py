"""TOTP helpers for KubeDock WebApp."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Optional

TOTP_PERIOD_SECONDS = 30
TOTP_DIGITS = 6
KEYRING_SERVICE = "KubeDock.WebApp.TOTP"
KEYRING_ACCOUNT = "primary"


class OTPConfigurationError(RuntimeError):
    """Secure OS credential vault is missing or unavailable."""


def generate_secret() -> str:
    """Generate a long-lived, random, Base32-encoded TOTP seed."""
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    if not isinstance(secret, str) or not secret:
        raise ValueError("TOTP secret is missing")
    return base64.b32decode(
        secret.upper() + "=" * ((8 - len(secret) % 8) % 8), casefold=True
    )


def current_counter(timestamp: Optional[float] = None) -> int:
    now = time.time() if timestamp is None else float(timestamp)
    if now < 0:
        raise ValueError("Timestamp cannot be negative")
    return int(now // TOTP_PERIOD_SECONDS)


def totp(secret: str, timestamp: Optional[float] = None) -> str:
    """RFC 6238 TOTP using HMAC-SHA1, 30-second steps, and six digits."""
    key = _decode_secret(secret)
    counter = current_counter(timestamp)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** TOTP_DIGITS)).zfill(TOTP_DIGITS)


def check_totp(
    secret: str,
    code: str,
    timestamp: Optional[float] = None,
    last_accepted_counter: Optional[int] = None,
) -> tuple[bool, str, Optional[int]]:
    """Validate only the current step and distinguish expired/replayed codes."""
    counter = current_counter(timestamp)
    if (
        not isinstance(code, str)
        or len(code) != TOTP_DIGITS
        or not code.isascii()
        or not code.isdigit()
    ):
        return False, "invalid", None

    current_code = totp(secret, counter * TOTP_PERIOD_SECONDS)
    if hmac.compare_digest(code, current_code):
        if last_accepted_counter is not None and counter <= last_accepted_counter:
            return False, "replayed", counter
        return True, "ok", counter

    previous = counter - 1
    if previous >= 0 and hmac.compare_digest(
        code, totp(secret, previous * TOTP_PERIOD_SECONDS)
    ):
        if last_accepted_counter is not None and previous == last_accepted_counter:
            return False, "replayed", previous
        return False, "expired", previous

    return False, "invalid", None


def get_or_create_secret() -> str:
    """Persist a stable seed in a supported OS keyring, never ordinary config."""
    try:
        import keyring
    except ImportError as exc:
        raise OTPConfigurationError("The keyring dependency is required for OTP") from exc

    try:
        backend = keyring.get_keyring()
        backend_module = type(backend).__module__.lower()
        supported = ("macos", "secretservice", "windows", "kwallet")
        if getattr(backend, "priority", 0) <= 0 or not any(
            name in backend_module for name in supported
        ):
            raise OTPConfigurationError(
                "A supported OS credential vault is required for WebApp OTP"
            )
        secret = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        if secret:
            _decode_secret(secret)
            return secret
        secret = generate_secret()
        keyring.set_password(KEYRING_SERVICE, KEYRING_ACCOUNT, secret)
        stored = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
        if not stored or not hmac.compare_digest(stored, secret):
            raise OTPConfigurationError("Unable to persist the OTP secret securely")
        return stored
    except OTPConfigurationError:
        raise
    except Exception as exc:
        raise OTPConfigurationError(
            "Unable to access the OS credential vault for WebApp OTP"
        ) from exc
