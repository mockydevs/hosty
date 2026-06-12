"""TOTP library (Phase 11d): RFC 6238 vectors, drift window, input hygiene."""

from __future__ import annotations

import base64

from app.core import totp

# RFC 6238 test secret: ASCII "12345678901234567890".
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()


def test_rfc6238_vectors():
    # RFC 6238 Appendix B values, truncated to 6 digits (the RFC lists 8).
    assert totp.totp_at(RFC_SECRET, 59) == "287082"
    assert totp.totp_at(RFC_SECRET, 1111111109) == "081804"
    assert totp.totp_at(RFC_SECRET, 1234567890) == "005924"
    assert totp.totp_at(RFC_SECRET, 2000000000) == "279037"


def test_verify_accepts_one_step_of_drift():
    now = 1_700_000_000
    assert totp.verify(RFC_SECRET, totp.totp_at(RFC_SECRET, now), timestamp=now)
    assert totp.verify(RFC_SECRET, totp.totp_at(RFC_SECRET, now - 30), timestamp=now)
    assert totp.verify(RFC_SECRET, totp.totp_at(RFC_SECRET, now + 30), timestamp=now)
    assert not totp.verify(RFC_SECRET, totp.totp_at(RFC_SECRET, now - 60), timestamp=now)
    assert not totp.verify(RFC_SECRET, totp.totp_at(RFC_SECRET, now + 60), timestamp=now)


def test_verify_rejects_garbage():
    now = 1_700_000_000
    assert not totp.verify(RFC_SECRET, "", timestamp=now)
    assert not totp.verify(RFC_SECRET, "12345", timestamp=now)  # too short
    assert not totp.verify(RFC_SECRET, "1234567", timestamp=now)  # too long
    assert not totp.verify(RFC_SECRET, "abcdef", timestamp=now)  # not digits
    assert not totp.verify(RFC_SECRET, "000000", timestamp=now) or True  # never raises


def test_verify_tolerates_spaces_in_code():
    now = 1_700_000_000
    code = totp.totp_at(RFC_SECRET, now)
    spaced = f"{code[:3]} {code[3:]}"
    assert totp.verify(RFC_SECRET, spaced, timestamp=now)


def test_generated_secret_round_trips():
    secret = totp.generate_secret()
    assert len(secret) == 32 and "=" not in secret  # 160 bits, unpadded base32
    now = 1_700_000_000
    assert totp.verify(secret, totp.totp_at(secret, now), timestamp=now)


def test_otpauth_uri():
    uri = totp.otpauth_uri("ABC234", username="don")
    assert uri.startswith("otpauth://totp/HostyPanel%3Adon?")
    assert "secret=ABC234" in uri
    assert "issuer=HostyPanel" in uri
    assert "digits=6" in uri and "period=30" in uri
