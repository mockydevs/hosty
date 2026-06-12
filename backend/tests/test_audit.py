"""Audit log (Week 22): mutating requests recorded, bodies never stored,
IP allowlist middleware, panel route in Caddy config."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.config import Settings
from app.db.models import AuditLog
from app.services.caddy import PanelSpec, build_config
from tests.conftest import TEST_PASSWORD, TEST_USER


async def _audit_rows(app) -> list[AuditLog]:
    async with app.state.sessionmaker() as db:
        return list((await db.execute(select(AuditLog).order_by(AuditLog.id))).scalars())


async def test_mutations_are_recorded_with_user(admin_client, app):
    resp = await admin_client.post(
        "/api/auth/change-password",
        json={
            "current_password": "wrong",
            "new_password": "x" * 12,
        },
    )
    assert resp.status_code == 401

    rows = await _audit_rows(app)
    paths = [(r.method, r.path, r.status_code) for r in rows]
    # setup + login (from the fixture) + the change-password attempt
    assert ("POST", "/api/auth/setup", 201) in paths
    assert ("POST", "/api/auth/login", 200) in paths
    assert ("POST", "/api/auth/change-password", 401) in paths
    change = next(r for r in rows if r.path == "/api/auth/change-password")
    assert change.username == TEST_USER and change.user_id is not None


async def test_reads_and_refresh_are_not_recorded(admin_client, app):
    await admin_client.get("/api/sites")
    await admin_client.post("/api/auth/refresh")
    rows = await _audit_rows(app)
    assert all(r.path != "/api/auth/refresh" for r in rows)
    assert all(r.method != "GET" for r in rows)


async def test_passwords_never_reach_the_audit_table(client, app):
    await client.post("/api/auth/setup", json={"username": TEST_USER, "password": TEST_PASSWORD})
    rows = await _audit_rows(app)
    dump = " ".join(f"{r.method} {r.path} {r.username}" for r in rows)
    assert TEST_PASSWORD not in dump  # bodies are never stored


async def test_failed_anonymous_login_is_recorded_without_user(client, app):
    await client.post("/api/auth/login", json={"username": "ghost", "password": "nope-nope"})
    rows = await _audit_rows(app)
    row = next(r for r in rows if r.path == "/api/auth/login")
    assert row.status_code == 401 and row.user_id is None and row.username is None


async def test_audit_api_pages_newest_first(admin_client):
    for domain in ["a-audit.example", "b-audit.example"]:
        await admin_client.post("/api/sites", json={"domain": domain})
    resp = await admin_client.get("/api/audit?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 4
    assert len(body["entries"]) == 2
    assert body["entries"][0]["id"] > body["entries"][1]["id"]


async def test_audit_requires_auth(client):
    assert (await client.get("/api/audit")).status_code == 401


# --- IP allowlist --------------------------------------------------------------


@pytest.fixture
def allowlist_settings() -> Settings:
    return Settings(
        env="test",
        secret_key="test-secret-key-at-least-32-bytes-long!",
        database_url="sqlite+aiosqlite:///:memory:",
        create_tables_on_startup=True,
        cookie_secure=False,
        panel_allowed_ips=["203.0.113.7"],
        caddy_sync_on_startup=False,
        _env_file=None,
    )


async def test_ip_allowlist_blocks_unknown_clients(allowlist_settings):
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app

    application = create_app(allowlist_settings)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application, client=("198.51.100.1", 1234))
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            resp = await c.get("/api/health")
            assert resp.status_code == 403
            assert resp.json()["error"]["code"] == "ip_not_allowed"

        transport = ASGITransport(app=application, client=("203.0.113.7", 1234))
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            assert (await c.get("/api/health")).status_code == 200


# --- panel behind Caddy ----------------------------------------------------------


def test_panel_route_snapshot():
    spec = PanelSpec(
        domain="panel.example.com",
        upstream="127.0.0.1:8800",
        allowed_ips=("203.0.113.7", "198.51.100.0/24"),
    )
    cfg = build_config([], panel=spec)
    assert cfg["apps"]["http"]["servers"]["hosty"]["routes"][0] == {
        "match": [
            {
                "host": ["panel.example.com"],
                "remote_ip": {"ranges": ["203.0.113.7", "198.51.100.0/24"]},
            }
        ],
        "handle": [{"handler": "reverse_proxy", "upstreams": [{"dial": "127.0.0.1:8800"}]}],
        "terminal": True,
    }
    # The panel route precedes all site routes and is absent without a spec.
    assert "remote_ip" not in str(build_config([]))


def test_panel_route_without_allowlist_has_no_ip_matcher():
    cfg = build_config([], panel=PanelSpec(domain="p.example", upstream="127.0.0.1:8800"))
    assert "remote_ip" not in cfg["apps"]["http"]["servers"]["hosty"]["routes"][0]["match"][0]


async def test_startup_publishes_panel_vhost_when_configured(monkeypatch):
    """Fresh install with HOSTY_PANEL_DOMAIN: Caddy gets the panel route at
    boot, before any site exists; Caddy being down must not break startup."""
    from httpx import ASGITransport, AsyncClient

    from app.main import create_app
    from app.services.caddy import CaddyClient, CaddyError

    applied: list[dict] = []

    async def fake_apply(self, config):
        applied.append(config)

    monkeypatch.setattr(CaddyClient, "apply", fake_apply)
    settings = Settings(
        env="test",
        secret_key="test-secret-key-at-least-32-bytes-long!",
        database_url="sqlite+aiosqlite:///:memory:",
        create_tables_on_startup=True,
        panel_domain="panel.example.com",
        _env_file=None,
    )
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        pass
    assert len(applied) == 1
    route = applied[0]["apps"]["http"]["servers"]["hosty"]["routes"][0]
    assert route["match"][0]["host"] == ["panel.example.com"]

    # Caddy down: startup still succeeds.
    async def down(self, config):
        raise CaddyError("unreachable")

    monkeypatch.setattr(CaddyClient, "apply", down)
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://t") as c:
            assert (await c.get("/api/health")).status_code == 200
