"""Shared GitHub App helpers (JWT + installation access tokens)."""

from __future__ import annotations

import time

import httpx
import jwt as pyjwt

from app.core.errors import ConflictError
from app.core.secrets import decrypt_secret


async def get_installation_token(source: object, settings: object) -> str:
    """Return a short-lived GitHub App installation access token.

    Works with any GitSource ORM row and a settings object that has
    `secret_key`.  Raises ConflictError if GitHub rejects the request.
    """
    private_key = decrypt_secret(source.private_key_encrypted, settings.secret_key)
    now = int(time.time())
    app_jwt = pyjwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": str(source.app_id)},
        private_key,
        algorithm="RS256",
    )
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.github.com/app/installations/{source.installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
    if resp.status_code != 201:
        raise ConflictError(f"Could not obtain GitHub installation token: {resp.text[:200]}")
    return resp.json()["token"]


