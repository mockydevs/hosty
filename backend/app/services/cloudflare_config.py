"""Cloudflare API token, managed in the UI and stored in the panel DB.

The token is encrypted at rest (app.core.secrets). The HOSTY_CLOUDFLARE_API_TOKEN
environment variable acts only as a bootstrap fallback when nothing is stored
in the DB.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret
from app.db.models import PanelSetting

log = structlog.get_logger("hosty.cloudflare_config")

CF_SETTINGS_KEY = "cloudflare"


@dataclass(frozen=True)
class CloudflareConfig:
    api_token: str
    source: str = "db"  # db | env


async def load(db: AsyncSession, settings: Settings) -> CloudflareConfig | None:
    """The active Cloudflare config: DB first, env fallback, else None."""
    row = await db.get(PanelSetting, CF_SETTINGS_KEY)
    if row is not None:
        data = json.loads(row.value)
        try:
            token = decrypt_secret(data["api_token_encrypted"], settings.secret_key)
        except SecretDecryptionError:
            log.error("cloudflare_token_undecryptable")
            return None
        return CloudflareConfig(api_token=token, source="db")
    if settings.cloudflare_api_token:
        return CloudflareConfig(api_token=settings.cloudflare_api_token, source="env")
    return None


async def save(db: AsyncSession, settings: Settings, *, api_token: str) -> CloudflareConfig:
    """Upsert the stored token (encrypted at rest)."""
    value = json.dumps(
        {"api_token_encrypted": encrypt_secret(api_token.strip(), settings.secret_key)}
    )
    row = await db.get(PanelSetting, CF_SETTINGS_KEY)
    if row is None:
        db.add(PanelSetting(key=CF_SETTINGS_KEY, value=value))
    else:
        row.value = value
    await db.commit()
    config = await load(db, settings)
    assert config is not None
    return config


async def clear(db: AsyncSession) -> bool:
    row = await db.get(PanelSetting, CF_SETTINGS_KEY)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True
