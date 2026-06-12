"""TOTP (RFC 6238) with the standard library only — no new dependency.

30-second period, 6 digits, SHA-1: the parameters every authenticator app
(Google Authenticator, Aegis, 1Password…) uses by default. Verification
accepts ±1 step of clock drift.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

PERIOD_SECONDS = 30
DIGITS = 6
DRIFT_STEPS = 1  # accept the previous/next 30s window


def generate_secret() -> str:
    """A 160-bit base32 secret (no padding), as authenticator apps expect."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(key: bytes, counter: int) -> str:
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**DIGITS)).zfill(DIGITS)


def _decode_secret(secret: str) -> bytes:
    padded = secret.strip().upper().replace(" ", "")
    padded += "=" * (-len(padded) % 8)
    return base64.b32decode(padded)


def totp_at(secret: str, timestamp: float) -> str:
    return _hotp(_decode_secret(secret), int(timestamp) // PERIOD_SECONDS)


def verify(secret: str, code: str, *, timestamp: float | None = None) -> bool:
    """Constant-time comparison across the drift window."""
    candidate = code.strip().replace(" ", "")
    if len(candidate) != DIGITS or not candidate.isdigit():
        return False
    now = time.time() if timestamp is None else timestamp
    counter = int(now) // PERIOD_SECONDS
    key = _decode_secret(secret)
    return any(
        hmac.compare_digest(_hotp(key, counter + step), candidate)
        for step in range(-DRIFT_STEPS, DRIFT_STEPS + 1)
    )


def otpauth_uri(secret: str, *, username: str, issuer: str = "HostyPanel") -> str:
    """The otpauth:// URI authenticator apps import (shown as a QR code)."""
    label = urllib.parse.quote(f"{issuer}:{username}")
    query = urllib.parse.urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": 30}
    )
    return f"otpauth://totp/{label}?{query}"
