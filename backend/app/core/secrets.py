"""Symmetric encryption for secrets stored in the panel DB.

Fernet (AES-128-CBC + HMAC) keyed deterministically from HOSTY_SECRET_KEY via
HKDF-SHA256, so the DB alone never reveals stored credentials. Changing the
secret key makes previously stored secrets unreadable — they must then be
re-entered.

MIGRATION NOTE: v2 switched from raw SHA-256 to HKDF. Any secrets encrypted
before this change must be re-entered (rotate SSH keys / re-deploy stacks).
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class SecretDecryptionError(ValueError):
    pass


def _fernet(secret_key: str) -> Fernet:
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"hosty-db-secrets-v2",
        info=b"fernet",
    ).derive(secret_key.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plain: str, secret_key: str) -> str:
    return _fernet(secret_key).encrypt(plain.encode()).decode()


def decrypt_secret(token: str, secret_key: str) -> str:
    try:
        return _fernet(secret_key).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise SecretDecryptionError(
            "Cannot decrypt a stored secret — was HOSTY_SECRET_KEY changed?"
        ) from exc
