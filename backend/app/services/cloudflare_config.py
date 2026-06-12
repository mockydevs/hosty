"""Cloudflare API tokens managed in the UI and stored in the panel DB.

Phase 11b: tokens are PER USER. The admin's token lives under the legacy
`cloudflare` key (kept for backwards compatibility with pre-11b installs);
each client's token lives under `cloudflare:{user_id}`. Every token is
encrypted at rest (app.core.secrets). The HOSTY_CLOUDFLARE_API_TOKEN
environment variable acts only as a bootstrap fallback for ADMINS when no
token is stored in the DB — clients never inherit it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret
from app.db.models import PanelSetting, User

log = structlog.get_logger("hosty.cloudflare_config")

CF_SETTINGS_KEY = "cloudflare"


def settings_key_for(user: User) -> str:
    """Admins share the panel-wide legacy key; clients get their own."""
    return CF_SETTINGS_KEY if user.role == "admin" else f"{CF_SETTINGS_KEY}:{user.id}"


@dataclass(frozen=True)
class CloudflareConfig:
    api_token: str
    source: str = "db"  # db | env


async def _load_key(db: AsyncSession, settings: Settings, key: str) -> CloudflareConfig | None:
    row = await db.get(PanelSetting, key)
    if row is None:
        return None
    data = json.loads(row.value)
    try:
        token = decrypt_secret(data["api_token_encrypted"], settings.secret_key)
    except SecretDecryptionError:
        log.error("cloudflare_token_undecryptable", key=key)
        return None
    return CloudflareConfig(api_token=token, source="db")


async def load_for_user(
    db: AsyncSession, settings: Settings, user: User
) -> CloudflareConfig | None:
    """The user's active Cloudflare config: their DB token, then (admins only)
    the env fallback, else None."""
    config = await _load_key(db, settings, settings_key_for(user))
    if config is not None:
        return config
    if user.role == "admin" and settings.cloudflare_api_token:
        return CloudflareConfig(api_token=settings.cloudflare_api_token, source="env")
    return None


async def load(db: AsyncSession, settings: Settings) -> CloudflareConfig | None:
    """The panel-wide (admin) Cloudflare config — pre-11b call sites."""
    config = await _load_key(db, settings, CF_SETTINGS_KEY)
    if config is not None:
        return config
    if settings.cloudflare_api_token:
        return CloudflareConfig(api_token=settings.cloudflare_api_token, source="env")
    return None


async def save_for_user(
    db: AsyncSession, settings: Settings, user: User, *, api_token: str
) -> CloudflareConfig:
    """Upsert the user's stored token (encrypted at rest)."""
    key = settings_key_for(user)
    value = json.dumps(
        {"api_token_encrypted": encrypt_secret(api_token.strip(), settings.secret_key)}
    )
    row = await db.get(PanelSetting, key)
    if row is None:
        db.add(PanelSetting(key=key, value=value))
    else:
        row.value = value
    await db.commit()
    config = await load_for_user(db, settings, user)
    assert config is not None
    return config


async def clear_for_user(db: AsyncSession, user: User) -> bool:
    row = await db.get(PanelSetting, settings_key_for(user))
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True
