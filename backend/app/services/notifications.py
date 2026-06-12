"""Admin notifications (Phase 11d) + the Week 21 dashboard surfacing.

`emit` writes a row unless an unresolved notification with the same
`dedupe_key` already exists (recurring checks must not flood the list);
`resolve` closes it when the condition clears. An optional webhook URL
(panel setting `notify_webhook_url`) receives a JSON POST per new
notification — best-effort, never blocking the caller on failure.
"""

from __future__ import annotations

import json

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.db.models import Notification, PanelSetting

log = structlog.get_logger("hosty.notifications")

WEBHOOK_SETTINGS_KEY = "notify_webhook_url"

KINDS = frozenset({"backup_failed", "service_down", "disk_full", "cert_failed", "quota_exceeded"})
SEVERITIES = frozenset({"info", "warning", "error"})


async def emit(
    db: AsyncSession,
    *,
    kind: str,
    message: str,
    severity: str = "warning",
    dedupe_key: str | None = None,
) -> Notification | None:
    """Create a notification; returns None when deduplicated."""
    if severity not in SEVERITIES:
        severity = "warning"
    if dedupe_key is not None:
        existing = (
            await db.execute(
                select(Notification).where(
                    Notification.dedupe_key == dedupe_key,
                    Notification.resolved_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None
    row = Notification(kind=kind, severity=severity, message=message, dedupe_key=dedupe_key)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    log.info("notification_emitted", kind=kind, severity=severity, message=message)
    await _webhook(db, row)
    return row


async def resolve(db: AsyncSession, dedupe_key: str) -> int:
    """Mark every unresolved notification with this key resolved (condition cleared)."""
    rows = (
        (
            await db.execute(
                select(Notification).where(
                    Notification.dedupe_key == dedupe_key,
                    Notification.resolved_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.resolved_at = utcnow()
    if rows:
        await db.commit()
    return len(rows)


async def _webhook(db: AsyncSession, notification: Notification) -> None:
    """POST the notification to the configured webhook, if any. Best-effort."""
    row = await db.get(PanelSetting, WEBHOOK_SETTINGS_KEY)
    if row is None:
        return
    try:
        url = json.loads(row.value)["url"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return
    payload = {
        "kind": notification.kind,
        "severity": notification.severity,
        "message": notification.message,
        "created_at": notification.created_at.isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json=payload)
    except httpx.HTTPError as exc:
        log.warning("notification_webhook_failed", error=str(exc))
