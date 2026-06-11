"""Symmetric encryption for secrets stored in the panel DB.

Fernet (AES-128-CBC + HMAC) keyed deterministically from HOSTY_SECRET_KEY, so
the DB alone never reveals stored credentials. Changing the secret key makes
previously stored secrets unreadable — they must then be re-entered.
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


class SecretDecryptionError(ValueError):
    pass


def _fernet(secret_key: str) -> Fernet:
    digest = hashlib.sha256(f"hosty-db-secrets:{secret_key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plain: str, secret_key: str) -> str:
    return _fernet(secret_key).encrypt(plain.encode()).decode()


def decrypt_secret(token: str, secret_key: str) -> str:
    try:
        return _fernet(secret_key).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise SecretDecryptionError(
            "Cannot decrypt a stored secret — was HOSTY_SECRET_KEY changed?"
        ) from exc
