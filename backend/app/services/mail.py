"""SMTP configuration and best-effort mail delivery."""

from __future__ import annotations

import asyncio
import json
import smtplib
import socket
import ssl
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
# "ipv4" (default) connects over IPv4 only. "any" additionally tries IPv6, but
# IPv4 is always attempted first so a broken IPv6 route can never block mail.
SMTP_IP_FAMILY = Literal["ipv4", "any"]


class SMTPVerifyError(Exception):
    """Raised when the SMTP server cannot be reached or credentials are rejected.

    Carries a human-readable message suitable for showing to the admin.
    """


def describe_smtp_error(exc: Exception) -> str:
    """Map a low-level SMTP/socket error to a short, actionable message."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "Authentication failed — check the login and password."
    if isinstance(exc, smtplib.SMTPConnectError):
        return "Could not connect to the mail server — check the host and port."
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "The mail server closed the connection unexpectedly — check the security mode."
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "The mail server does not support the selected security mode."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "The mail server refused the recipient address."
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "The mail server refused the sender address — check the sender email."
    if isinstance(exc, smtplib.SMTPException):
        return str(exc) or "The mail server rejected the request."
    if isinstance(exc, ssl.SSLError):
        return "TLS handshake failed — check the security mode (STARTTLS vs SSL/TLS) and port."
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Timed out reaching the mail server — check the host, port, and firewall."
    if isinstance(exc, socket.gaierror):
        return "Could not resolve the mail server hostname — check the host."
    if isinstance(exc, ConnectionRefusedError):
        return "Connection refused — check the host and port."
    if isinstance(exc, OSError):
        return f"Network error reaching the mail server: {exc}"
    return str(exc) or exc.__class__.__name__


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
    ip_family: SMTP_IP_FAMILY = "ipv4"


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
        ip_family=data.get("ip_family", "ipv4"),
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
    ip_family: SMTP_IP_FAMILY = "ipv4",
) -> SMTPConfig:
    if security not in ("starttls", "ssl", "none"):
        raise ConflictError("Unsupported SMTP security mode")
    if ip_family not in ("ipv4", "any"):
        raise ConflictError("Unsupported SMTP IP family")
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
            "ip_family": ip_family,
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


def _connect(host: str, port: int, timeout: float, ip_family: SMTP_IP_FAMILY) -> socket.socket:
    """Open a TCP connection, trying IPv4 first and reporting every failure.

    Unlike ``socket.create_connection`` (which raises only the *last* address's
    error — typically masking a useful IPv4 failure behind an IPv6 one, or vice
    versa), this collects each attempt so the admin sees the full picture.
    """
    family = socket.AF_INET if ip_family == "ipv4" else socket.AF_UNSPEC
    infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    # IPv4 must always be attempted, and attempted first: a half-configured
    # IPv6 stack on the panel host must never block outgoing mail.
    infos.sort(key=lambda info: 0 if info[0] == socket.AF_INET else 1)
    failures: list[str] = []
    for af, socktype, proto, _, sa in infos:
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            sock.settimeout(timeout)
            sock.connect(sa)
            return sock
        except OSError as exc:
            failures.append(f"{sa[0]} ({'IPv6' if af == socket.AF_INET6 else 'IPv4'}): {exc}")
            if sock is not None:
                sock.close()
    raise OSError("could not connect — " + "; ".join(failures))


class _SMTP(smtplib.SMTP):
    """smtplib.SMTP with address-family control and per-address errors."""

    def __init__(self, *args, ip_family: SMTP_IP_FAMILY = "ipv4", **kwargs):
        self._ip_family: SMTP_IP_FAMILY = ip_family
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        return _connect(host, port, timeout, self._ip_family)


class _SMTP_SSL(smtplib.SMTP_SSL):
    """smtplib.SMTP_SSL with address-family control and per-address errors."""

    def __init__(self, *args, ip_family: SMTP_IP_FAMILY = "ipv4", **kwargs):
        self._ip_family: SMTP_IP_FAMILY = ip_family
        super().__init__(*args, **kwargs)

    def _get_socket(self, host, port, timeout):
        sock = _connect(host, port, timeout, self._ip_family)
        return self.context.wrap_socket(sock, server_hostname=self._host)


def _smtp_client(config: SMTPConfig) -> smtplib.SMTP:
    cls = _SMTP_SSL if config.security == "ssl" else _SMTP
    return cls(config.host, config.port, timeout=10, ip_family=config.ip_family)


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
    with _smtp_client(config) as smtp:
        smtp.ehlo()
        if config.security == "starttls":
            smtp.starttls()
            smtp.ehlo()
        if config.username:
            smtp.login(config.username, config.password)
        smtp.send_message(message)


async def verify(config: SMTPConfig) -> None:
    """Connect, negotiate TLS, and authenticate without sending a message.

    This confirms the stored host, port, security mode, and (when a username is
    set) the login/password are accepted by the server. Raises
    :class:`SMTPVerifyError` with a human-readable message on any failure.
    """
    try:
        await asyncio.to_thread(_verify_sync, config)
    except Exception as exc:
        log.info("smtp_verify_failed", host=config.host, error=str(exc))
        raise SMTPVerifyError(describe_smtp_error(exc)) from exc


def _verify_sync(config: SMTPConfig) -> None:
    with _smtp_client(config) as smtp:
        smtp.ehlo()
        if config.security == "starttls":
            smtp.starttls()
            smtp.ehlo()
        if config.username:
            smtp.login(config.username, config.password)
        smtp.noop()


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
