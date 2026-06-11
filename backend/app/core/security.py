"""Password hashing (Argon2id) and token primitives (JWT access, opaque refresh)."""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.errors import UnauthorizedError

_hasher = PasswordHasher()  # Argon2id, library-recommended parameters

JWT_ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def create_access_token(*, subject: str, secret: str, ttl_seconds: int) -> str:
    now = int(time.time())
    payload = {
        "sub": subject,
        "iat": now,
        "exp": now + ttl_seconds,
        "type": "access",
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, *, secret: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "iat", "exp"]},
        )
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("Invalid or expired token") from exc
    if payload.get("type") != "access":
        raise UnauthorizedError("Invalid token type")
    return payload


def generate_refresh_token() -> str:
    """Opaque 384-bit random token. Only its SHA-256 hash is ever stored."""
    return secrets.token_urlsafe(48)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
