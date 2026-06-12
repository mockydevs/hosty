"""SMTP configuration and best-effort mail delivery."""

from __future__ import annotations

import asyncio
import json
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError
from app.core.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret
from app.db.models import Notification, PanelSetting, User

log = structlog.get_logger("hosty.mail")

SMTP_SETTINGS_KEY = "smtp"
SMTP_SECURITY = Literal["starttls", "ssl", "none"]


@dataclass(frozen=True)
class SMTPConfig:
    host: str
    port: int
    from_email: str
    from_name: str = "Hosty"
    security: SMTP_SECURITY = "starttls"
    username: str = ""
    password: str = ""
    notification_recipients: tuple[str, ...] = ()


def normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    email = value.strip().lower()
    if not email:
        return None
    _, parsed = parseaddr(email)
    if parsed != email or "@" not in parsed or parsed.startswith("@") or parsed.endswith("@"):
        raise ValueError("Invalid email address")
    local, domain = parsed.rsplit("@", 1)
    if not local or "." not in domain:
        raise ValueError("Invalid email address")
    return parsed


def normalize_emails(values: list[str]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        email = normalize_email(value)
        if email is not None:
            out.append(email)
    return tuple(dict.fromkeys(out))


async def load(db: AsyncSession, settings: Settings) -> SMTPConfig | None:
    row = await db.get(PanelSetting, SMTP_SETTINGS_KEY)
    if row is None:
        return None
    data = json.loads(row.value)
    password = ""
    encrypted = data.get("password_encrypted")
    if encrypted:
        try:
            password = decrypt_secret(encrypted, settings.secret_key)
        except SecretDecryptionError:
            log.error("smtp_password_undecryptable")
            return None
    return SMTPConfig(
        host=data["host"],
        port=int(data["port"]),
        from_email=data["from_email"],
        from_name=data.get("from_name", "Hosty"),
        security=data.get("security", "starttls"),
        username=data.get("username", ""),
        password=password,
        notification_recipients=tuple(data.get("notification_recipients", [])),
    )


async def save(
    db: AsyncSession,
    settings: Settings,
    *,
    host: str,
    port: int,
    from_email: str,
    from_name: str = "Hosty",
    security: SMTP_SECURITY = "starttls",
    username: str = "",
    password: str | None = None,
    notification_recipients: list[str] | None = None,
) -> SMTPConfig:
    if security not in ("starttls", "ssl", "none"):
        raise ConflictError("Unsupported SMTP security mode")
    current = await load(db, settings)
    if password is None or password == "":
        password = current.password if current is not None else ""
    value = json.dumps(
        {
            "host": host.strip(),
            "port": int(port),
            "from_email": normalize_email(from_email),
            "from_name": from_name.strip() or "Hosty",
            "security": security,
            "username": username.strip(),
            "password_encrypted": encrypt_secret(password, settings.secret_key) if password else "",
            "notification_recipients": list(normalize_emails(notification_recipients or [])),
        }
    )
    row = await db.get(PanelSetting, SMTP_SETTINGS_KEY)
    if row is None:
        db.add(PanelSetting(key=SMTP_SETTINGS_KEY, value=value))
    else:
        row.value = value
    await db.commit()
    config = await load(db, settings)
    assert config is not None
    return config


async def clear(db: AsyncSession) -> bool:
    row = await db.get(PanelSetting, SMTP_SETTINGS_KEY)
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


async def send(config: SMTPConfig, *, to: str, subject: str, text: str) -> None:
    recipient = normalize_email(to)
    if recipient is None:
        raise ValueError("Recipient email is required")
    message = EmailMessage()
    message["From"] = formataddr((config.from_name, config.from_email))
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text)
    await asyncio.to_thread(_send_sync, config, message)


def _send_sync(config: SMTPConfig, message: EmailMessage) -> None:
    smtp_cls = smtplib.SMTP_SSL if config.security == "ssl" else smtplib.SMTP
    with smtp_cls(config.host, config.port, timeout=10) as smtp:
        smtp.ehlo()
        if config.security == "starttls":
            smtp.starttls()
            smtp.ehlo()
        if config.username:
            smtp.login(config.username, config.password)
        smtp.send_message(message)


async def send_user_temp_password(
    db: AsyncSession, settings: Settings, *, user: User, temp_password: str
) -> bool:
    if user.email is None:
        return False
    config = await load(db, settings)
    if config is None:
        return False
    try:
        await send(
            config,
            to=user.email,
            subject="Your Hosty account temporary password",
            text=(
                f"Hello {user.username},\n\n"
                "Your Hosty account has a new temporary password:\n\n"
                f"{temp_password}\n\n"
                "Sign in and change it before using the panel."
            ),
        )
        return True
    except Exception as exc:
        log.warning("temp_password_email_failed", username=user.username, error=str(exc))
        return False


async def send_notification_email(
    db: AsyncSession, settings: Settings, notification: Notification
) -> None:
    config = await load(db, settings)
    if config is None or not config.notification_recipients:
        return
    subject = f"Hosty notification: {notification.severity} {notification.kind}"
    text = (
        f"{notification.message}\n\n"
        f"Kind: {notification.kind}\n"
        f"Severity: {notification.severity}\n"
        f"Created: {notification.created_at.isoformat()}\n"
    )
    for recipient in config.notification_recipients:
        try:
            await send(config, to=recipient, subject=subject, text=text)
        except Exception as exc:
            log.warning("notification_email_failed", recipient=recipient, error=str(exc))
