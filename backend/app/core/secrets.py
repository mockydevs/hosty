"""Symmetric encryption for secrets stored in the panel DB.

Fernet (AES-128-CBC + HMAC) keyed deterministically from HOSTY_SECRET_KEY via
HKDF-SHA256, so the DB alone never reveals stored credentials. Changing the
secret key makes previously stored secrets unreadable — they must then be
re-entered.

KDF history
-----------
v1 (pre-HKDF): key = base64(sha256(secret_key))  — no salt, no context
v2 (current):  key = base64(HKDF-SHA256(secret_key, salt="hosty-db-secrets-v2"))

decrypt_secret() tries v2 first, then v1, so data encrypted before the migration
is still readable without any manual re-entry. It raises SecretDecryptionError
only when both attempts fail (genuine key rotation or corruption).
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SecretDecryptionError(ValueError):
    pass


# ── v2 KDF (current) ──────────────────────────────────────────────────────────

def _fernet(secret_key: str) -> Fernet:
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"hosty-db-secrets-v2",
        info=b"fernet",
    ).derive(secret_key.encode())
    return Fernet(base64.urlsafe_b64encode(key))


# ── v1 KDF (legacy, migration fallback) ──────────────────────────────────────

def _fernet_v1(secret_key: str) -> Fernet:
    """Pre-HKDF key derivation: single-pass SHA-256 without salt or context.
    Used only as a read-fallback so data encrypted before the v2 migration can
    still be decrypted without re-entry."""
    key = base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest())
    return Fernet(key)


# ── Public API ────────────────────────────────────────────────────────────────

def encrypt_secret(plain: str, secret_key: str) -> str:
    """Always encrypts with the current (v2 HKDF) key."""
    return _fernet(secret_key).encrypt(plain.encode()).decode()


def decrypt_secret(token: str, secret_key: str) -> str:
    """Decrypt a Fernet token.

    Tries the current v2 KDF first. If that fails, tries the legacy v1 SHA-256
    KDF so data encrypted before the HKDF migration is transparently readable.
    Raises SecretDecryptionError only when both attempts fail.
    """
    # v2 HKDF (all newly written tokens)
    try:
        return _fernet(secret_key).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        pass

    # v1 SHA-256 fallback (tokens written before the HKDF migration)
    try:
        return _fernet_v1(secret_key).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise SecretDecryptionError(
            "Cannot decrypt a stored secret — was HOSTY_SECRET_KEY changed?"
        ) from exc
