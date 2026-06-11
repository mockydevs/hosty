"""S3 backup-target configuration, managed in the UI and stored in the panel DB.

The secret access key is encrypted at rest (app.core.secrets). Environment
variables (HOSTY_S3_*) act only as a bootstrap fallback when nothing is stored
in the DB.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError
from app.core.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret
from app.db.models import PanelSetting

log = structlog.get_logger("hosty.s3_config")

S3_SETTINGS_KEY = "s3"


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = ""
    prefix: str = "hosty"
    source: str = "db"  # db | env


async def load(db: AsyncSession, settings: Settings) -> S3Config | None:
    """The active S3 config: DB first, env fallback, else None."""
    row = await db.get(PanelSetting, S3_SETTINGS_KEY)
    if row is not None:
        data = json.loads(row.value)
        try:
            secret = decrypt_secret(data["secret_key_encrypted"], settings.secret_key)
        except SecretDecryptionError:
            log.error("s3_secret_undecryptable")
            return None
        return S3Config(
            endpoint=data["endpoint"],
            bucket=data["bucket"],
            access_key=data["access_key"],
            secret_key=secret,
            region=data.get("region", ""),
            prefix=data.get("prefix", "hosty"),
            source="db",
        )
    if (
        settings.s3_endpoint
        and settings.s3_bucket
        and settings.s3_access_key
        and settings.s3_secret_key
    ):
        return S3Config(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region=settings.s3_region,
            prefix=settings.s3_prefix,
            source="env",
        )
    return None


async def save(
    db: AsyncSession,
    settings: Settings,
    *,
    endpoint: str,
    bucket: str,
    access_key: str,
    secret_key: str | None,
    region: str = "",
    prefix: str = "hosty",
) -> S3Config:
    """Upsert the stored config. `secret_key=None` keeps the existing secret."""
    if secret_key is None or secret_key == "":
        current = await load(db, settings)
        if current is None:
            raise ConflictError("A secret access key is required the first time")
        secret_key = current.secret_key
    value = json.dumps(
        {
            "endpoint": endpoint.strip().rstrip("/"),
            "bucket": bucket.strip(),
            "access_key": access_key.strip(),
            "secret_key_encrypted": encrypt_secret(secret_key, settings.secret_key),
            "region": region.strip(),
            "prefix": prefix.strip().strip("/") or "hosty",
        }
    )
    row = await db.get(PanelSetting, S3_SETTINGS_KEY)
    if row is None:
        db.add(PanelSetting(key=S3_SETTINGS_KEY, value=value))
    else:
        row.value = value
    await db.commit()
    config = await load(db, settings)
    assert config is not None
    return config


async def clear(db: AsyncSession) -> bool:
    row = await db.get(PanelSetting, S3_SETTINGS_KEY)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True
