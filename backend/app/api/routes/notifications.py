"""Admin notifications API (Phase 11d) + webhook configuration.

The dashboard shows unresolved notifications; admins can mark them read,
dismiss them, and point an optional webhook (e.g. Slack-compatible receiver)
at the panel for email/webhook-style delivery.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, HttpUrl
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.clock import utcnow
from app.core.errors import NotFoundError
from app.db.models import Notification, PanelSetting
from app.services.notifications import WEBHOOK_SETTINGS_KEY

router = APIRouter(dependencies=[Depends(require_admin)])


class NotificationResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    kind: str
    severity: str
    message: str
    read: bool
    created_at: datetime
    resolved_at: datetime | None


@router.get("", response_model=list[NotificationResponse])
async def list_notifications(
    include_resolved: bool = False,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
) -> Any:
    query = select(Notification).order_by(Notification.created_at.desc())
    if not include_resolved:
        query = query.where(Notification.resolved_at.is_(None))
    query = query.limit(min(max(int(limit), 1), 500))
    return (await db.execute(query)).scalars().all()


@router.post("/{notification_id:int}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(notification_id: int, db: AsyncSession = Depends(get_db)) -> None:
    row = await db.get(Notification, notification_id)
    if row is None:
        raise NotFoundError("Notification not found")
    row.read = True
    await db.commit()


# `:int` so the literal /webhook routes below can never be shadowed.
@router.delete("/{notification_id:int}", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss(notification_id: int, db: AsyncSession = Depends(get_db)) -> None:
    """Dismiss = resolve by hand. The dedupe key frees up, so the condition
    re-notifies if it is still present at the next sweep."""
    row = await db.get(Notification, notification_id)
    if row is None:
        raise NotFoundError("Notification not found")
    row.resolved_at = utcnow()
    row.read = True
    await db.commit()


# --- webhook config ------------------------------------------------------------------


class WebhookConfigResponse(BaseModel):
    configured: bool
    url: str | None = None


class UpdateWebhookRequest(BaseModel):
    url: HttpUrl


@router.get("/webhook", response_model=WebhookConfigResponse)
async def get_webhook(db: AsyncSession = Depends(get_db)) -> WebhookConfigResponse:
    row = await db.get(PanelSetting, WEBHOOK_SETTINGS_KEY)
    if row is None:
        return WebhookConfigResponse(configured=False)
    try:
        url = json.loads(row.value).get("url")
    except json.JSONDecodeError:
        url = None
    return WebhookConfigResponse(configured=url is not None, url=url)


@router.put("/webhook", response_model=WebhookConfigResponse)
async def set_webhook(
    body: UpdateWebhookRequest, db: AsyncSession = Depends(get_db)
) -> WebhookConfigResponse:
    value = json.dumps({"url": str(body.url)})
    row = await db.get(PanelSetting, WEBHOOK_SETTINGS_KEY)
    if row is None:
        db.add(PanelSetting(key=WEBHOOK_SETTINGS_KEY, value=value))
    else:
        row.value = value
    await db.commit()
    return WebhookConfigResponse(configured=True, url=str(body.url))


@router.delete("/webhook", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(db: AsyncSession = Depends(get_db)) -> None:
    row = await db.get(PanelSetting, WEBHOOK_SETTINGS_KEY)
    if row is None:
        raise NotFoundError("No webhook configured")
    await db.delete(row)
    await db.commit()
