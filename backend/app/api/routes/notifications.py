"""Admin notifications API (Phase 11d) + webhook configuration.

The dashboard shows unresolved notifications; admins can mark them read,
dismiss them, and point an optional webhook (e.g. Slack-compatible receiver)
at the panel for email/webhook-style delivery.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_admin
from app.core.clock import utcnow
from app.core.errors import ConflictError, NotFoundError
from app.db.models import Notification, PanelSetting
from app.services import mail
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


# --- SMTP config ---------------------------------------------------------------------


class SMTPConfigResponse(BaseModel):
    configured: bool
    host: str = ""
    port: int = 587
    from_email: str = ""
    from_name: str = "Hosty"
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str = ""
    notification_recipients: list[str] = Field(default_factory=list)
    has_password: bool = False


class UpdateSMTPConfigRequest(BaseModel):
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    from_email: str = Field(max_length=254)
    from_name: str = Field(default="Hosty", max_length=80)
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, max_length=512)
    notification_recipients: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("from_email")
    @classmethod
    def validate_from_email(cls, value: str) -> str:
        normalized = mail.normalize_email(value)
        if normalized is None:
            raise ValueError("From email is required")
        return normalized

    @field_validator("notification_recipients")
    @classmethod
    def validate_recipients(cls, values: list[str]) -> list[str]:
        return list(mail.normalize_emails(values))


class TestSMTPRequest(BaseModel):
    to: str = Field(max_length=254)

    @field_validator("to")
    @classmethod
    def validate_to(cls, value: str) -> str:
        normalized = mail.normalize_email(value)
        if normalized is None:
            raise ValueError("Recipient email is required")
        return normalized


def _smtp_response(config: mail.SMTPConfig | None) -> SMTPConfigResponse:
    if config is None:
        return SMTPConfigResponse(configured=False)
    return SMTPConfigResponse(
        configured=True,
        host=config.host,
        port=config.port,
        from_email=config.from_email,
        from_name=config.from_name,
        security=config.security,
        username=config.username,
        notification_recipients=list(config.notification_recipients),
        has_password=bool(config.password),
    )


@router.get("/smtp", response_model=SMTPConfigResponse)
async def get_smtp(request: Request, db: AsyncSession = Depends(get_db)) -> SMTPConfigResponse:
    return _smtp_response(await mail.load(db, request.app.state.settings))


@router.put("/smtp", response_model=SMTPConfigResponse)
async def set_smtp(
    request: Request, body: UpdateSMTPConfigRequest, db: AsyncSession = Depends(get_db)
) -> SMTPConfigResponse:
    config = await mail.save(
        db,
        request.app.state.settings,
        host=body.host,
        port=body.port,
        from_email=body.from_email,
        from_name=body.from_name,
        security=body.security,
        username=body.username,
        password=body.password,
        notification_recipients=body.notification_recipients,
    )
    return _smtp_response(config)


@router.post("/smtp/test", response_model=dict[str, bool])
async def test_smtp(
    request: Request, body: TestSMTPRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    config = await mail.load(db, request.app.state.settings)
    if config is None:
        raise NotFoundError("No SMTP configuration stored")
    try:
        await mail.send(
            config,
            to=body.to,
            subject="Hosty SMTP test",
            text="This is a test email from Hosty.",
        )
    except Exception as exc:
        raise ConflictError(f"SMTP Error: {exc}")
    return {"sent": True}


@router.delete("/smtp", status_code=status.HTTP_204_NO_CONTENT)
async def delete_smtp(db: AsyncSession = Depends(get_db)) -> None:
    if not await mail.clear(db):
        raise NotFoundError("No SMTP configuration stored")
