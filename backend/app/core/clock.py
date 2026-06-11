"""Time helpers. SQLite stores naive datetimes; we standardize on naive UTC."""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Naive UTC timestamp."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utc_timestamp(dt: datetime) -> int:
    """Unix timestamp for a naive-UTC datetime."""
    return int(dt.replace(tzinfo=timezone.utc).timestamp())
