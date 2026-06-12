"""Admin notifications (Phase 11d): dedupe/resolve semantics, API, webhook config."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import Notification
from app.services import mail as mail_service
from app.services import notifications
from tests.test_multi_tenancy import make_active_client


async def _rows(db) -> list[Notification]:
    return list((await db.execute(select(Notification))).scalars().all())


# --- service semantics ----------------------------------------------------------------


async def test_emit_dedupes_until_resolved(app):
    async with app.state.sessionmaker() as db:
        first = await notifications.emit(
            db, kind="disk_full", message="disk 95% full", dedupe_key="disk_full:/"
        )
        assert first is not None
        # Same key, unresolved -> swallowed.
        assert (
            await notifications.emit(
                db, kind="disk_full", message="disk 96% full", dedupe_key="disk_full:/"
            )
            is None
        )
        assert len(await _rows(db)) == 1

        assert await notifications.resolve(db, "disk_full:/") == 1
        # Condition returned after resolution -> a new row.
        again = await notifications.emit(
            db, kind="disk_full", message="disk 97% full", dedupe_key="disk_full:/"
        )
        assert again is not None and again.id != first.id


async def test_emit_without_dedupe_key_always_writes(app):
    async with app.state.sessionmaker() as db:
        assert await notifications.emit(db, kind="backup_failed", message="a") is not None
        assert await notifications.emit(db, kind="backup_failed", message="b") is not None
        assert len(await _rows(db)) == 2


async def test_emit_normalizes_unknown_severity(app):
    async with app.state.sessionmaker() as db:
        row = await notifications.emit(db, kind="disk_full", message="x", severity="apocalyptic")
        assert row is not None and row.severity == "warning"


async def test_resolve_unknown_key_is_noop(app):
    async with app.state.sessionmaker() as db:
        assert await notifications.resolve(db, "never-emitted") == 0


# --- API ------------------------------------------------------------------------------


async def test_notifications_api_lifecycle(admin_client, app):
    async with app.state.sessionmaker() as db:
        await notifications.emit(
            db,
            kind="service_down",
            message="caddy is inactive",
            severity="error",
            dedupe_key="service_down:caddy",
        )

    resp = await admin_client.get("/api/notifications")
    assert resp.status_code == 200
    (item,) = resp.json()
    assert item["kind"] == "service_down"
    assert item["read"] is False

    assert (await admin_client.post(f"/api/notifications/{item['id']}/read")).status_code == 204
    (item,) = (await admin_client.get("/api/notifications")).json()
    assert item["read"] is True

    # Dismiss = resolve by hand; default list hides it, include_resolved shows it.
    assert (await admin_client.delete(f"/api/notifications/{item['id']}")).status_code == 204
    assert (await admin_client.get("/api/notifications")).json() == []
    shown = (await admin_client.get("/api/notifications?include_resolved=true")).json()
    assert len(shown) == 1 and shown[0]["resolved_at"] is not None


async def test_notifications_404s(admin_client):
    assert (await admin_client.post("/api/notifications/999/read")).status_code == 404
    assert (await admin_client.delete("/api/notifications/999")).status_code == 404


async def test_notifications_are_admin_only(admin_client, client):
    headers = await make_active_client(admin_client, client)
    assert (await client.get("/api/notifications", headers=headers)).status_code == 403
    assert (await client.get("/api/notifications/webhook", headers=headers)).status_code == 403


# --- webhook config -------------------------------------------------------------------


async def test_webhook_config_roundtrip(admin_client):
    resp = await admin_client.get("/api/notifications/webhook")
    assert resp.json() == {"configured": False, "url": None}

    resp = await admin_client.put(
        "/api/notifications/webhook", json={"url": "https://hooks.example/notify"}
    )
    assert resp.status_code == 200
    assert resp.json()["configured"] is True

    resp = await admin_client.get("/api/notifications/webhook")
    assert resp.json()["url"] == "https://hooks.example/notify"

    assert (await admin_client.delete("/api/notifications/webhook")).status_code == 204
    assert (await admin_client.delete("/api/notifications/webhook")).status_code == 404


async def test_webhook_rejects_garbage_url(admin_client):
    resp = await admin_client.put("/api/notifications/webhook", json={"url": "not a url"})
    assert resp.status_code == 422


# --- SMTP config ----------------------------------------------------------------------


async def test_smtp_config_roundtrip_and_test_email(admin_client, monkeypatch):
    sent = []

    async def fake_send(config, *, to, subject, text):
        sent.append((config.host, to, subject, text))

    verified = []

    async def fake_verify(config):
        verified.append(config.host)

    monkeypatch.setattr(mail_service, "send", fake_send)
    monkeypatch.setattr(mail_service, "verify", fake_verify)

    assert (await admin_client.get("/api/notifications/smtp")).json()["configured"] is False

    resp = await admin_client.put(
        "/api/notifications/smtp",
        json={
            "host": "smtp.example.com",
            "port": 587,
            "from_email": "Panel@Example.com",
            "from_name": "Hosty Panel",
            "security": "starttls",
            "username": "smtp-user",
            "password": "smtp-secret",
            "notification_recipients": ["Admin@Example.com", "admin@example.com"],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    assert body["from_email"] == "panel@example.com"
    assert body["has_password"] is True
    assert body["notification_recipients"] == ["admin@example.com"]
    # Omitted in the request → defaults to IPv4-only.
    assert body["ip_family"] == "ipv4"
    # Save verifies the stored credentials and reports the result.
    assert body["verified"] is True
    assert body["verify_error"] is None
    assert verified == ["smtp.example.com"]

    resp = await admin_client.post("/api/notifications/smtp/test", json={"to": "ops@example.com"})
    assert resp.status_code == 200
    assert sent == [
        (
            "smtp.example.com",
            "ops@example.com",
            "Hosty SMTP test",
            "This is a test email from Hosty.",
        )
    ]

    assert (await admin_client.delete("/api/notifications/smtp")).status_code == 204
    assert (await admin_client.delete("/api/notifications/smtp")).status_code == 404


async def test_smtp_save_reports_unverified_credentials(admin_client, monkeypatch):
    async def failing_verify(config):
        raise mail_service.SMTPVerifyError("Authentication failed — check the login and password.")

    monkeypatch.setattr(mail_service, "verify", failing_verify)

    resp = await admin_client.put(
        "/api/notifications/smtp",
        json={
            "host": "smtp.example.com",
            "port": 587,
            "from_email": "panel@example.com",
            "security": "starttls",
            "username": "smtp-user",
            "password": "wrong-secret",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["configured"] is True
    assert body["verified"] is False
    assert "Authentication failed" in body["verify_error"]
    assert (await admin_client.get("/api/notifications/smtp")).json()["configured"] is True


async def test_smtp_ip_family_roundtrip_and_validation(admin_client, monkeypatch):
    async def fake_verify(config):
        return None

    monkeypatch.setattr(mail_service, "verify", fake_verify)

    base = {
        "host": "smtp.example.com",
        "port": 587,
        "from_email": "panel@example.com",
        "security": "starttls",
    }
    resp = await admin_client.put("/api/notifications/smtp", json={**base, "ip_family": "any"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["ip_family"] == "any"
    # Persisted, not just echoed.
    assert (await admin_client.get("/api/notifications/smtp")).json()["ip_family"] == "any"
    # IPv6-only is not a thing: IPv4 can never be turned off.
    resp = await admin_client.put("/api/notifications/smtp", json={**base, "ip_family": "ipv6"})
    assert resp.status_code == 422


def test_connect_tries_ipv4_first_and_reports_all_failures(monkeypatch):
    import socket as socket_mod

    attempts = []

    addrinfo = [
        # getaddrinfo commonly returns IPv6 first; _connect must reorder.
        (socket_mod.AF_INET6, socket_mod.SOCK_STREAM, 6, "", ("2001:db8::1", 587, 0, 0)),
        (socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", ("192.0.2.10", 587)),
    ]
    monkeypatch.setattr(mail_service.socket, "getaddrinfo", lambda *a, **k: list(addrinfo))

    class FailingSocket:
        def __init__(self, af, socktype, proto):
            self.af = af

        def settimeout(self, value):
            pass

        def connect(self, sa):
            attempts.append(sa[0])
            raise OSError(101, "Network is unreachable")

        def close(self):
            pass

    monkeypatch.setattr(mail_service.socket, "socket", FailingSocket)

    try:
        mail_service._connect("smtp.example.com", 587, 10, "any")
    except OSError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected OSError")

    # IPv4 attempted first, and both failures are visible to the admin.
    assert attempts == ["192.0.2.10", "2001:db8::1"]
    assert "192.0.2.10 (IPv4)" in message
    assert "2001:db8::1 (IPv6)" in message


def test_connect_ipv4_mode_requests_ipv4_only(monkeypatch):
    import socket as socket_mod

    families = []

    def fake_getaddrinfo(host, port, family, type):
        families.append(family)
        return []

    monkeypatch.setattr(mail_service.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(OSError):
        mail_service._connect("smtp.example.com", 587, 10, "ipv4")
    assert families == [socket_mod.AF_INET]


def test_describe_smtp_error_maps_common_failures():
    import smtplib
    import socket

    assert "Authentication" in mail_service.describe_smtp_error(
        smtplib.SMTPAuthenticationError(535, b"bad")
    )
    assert "resolve" in mail_service.describe_smtp_error(socket.gaierror())
    assert "refused" in mail_service.describe_smtp_error(ConnectionRefusedError()).lower()


async def test_notification_emit_can_email_recipients(admin_client, app, monkeypatch):
    sent = []

    async def fake_send(config, *, to, subject, text):
        sent.append((to, subject, text))

    async def fake_verify(config):
        return None

    monkeypatch.setattr(mail_service, "send", fake_send)
    monkeypatch.setattr(mail_service, "verify", fake_verify)
    await admin_client.put(
        "/api/notifications/smtp",
        json={
            "host": "smtp.example.com",
            "port": 587,
            "from_email": "panel@example.com",
            "security": "starttls",
            "notification_recipients": ["ops@example.com"],
        },
    )
    async with app.state.sessionmaker() as db:
        await notifications.emit(
            db,
            kind="disk_full",
            severity="error",
            message="Server disk is 95% full",
            settings=app.state.settings,
        )

    assert len(sent) == 1
    assert sent[0][0] == "ops@example.com"
    assert "disk_full" in sent[0][1]
