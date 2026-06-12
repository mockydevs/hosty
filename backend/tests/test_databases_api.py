"""Databases API: lifecycle, show-once credentials, orphans, Adminer proxy."""

from __future__ import annotations

import httpx
import pytest

from app.services import adminer as adminer_service
from app.services import mariadb
from app.services.adminer import issue_token, verify_token
from tests.test_sites_api import FakeSystem


@pytest.fixture
def fake_system(monkeypatch) -> FakeSystem:
    fake = FakeSystem()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def fake_mariadb(monkeypatch, fake_system):
    """Extends FakeSystem with reset/list operations for the databases API."""
    state = {"resets": [], "physical": None}

    async def reset_password(user: str, password: str) -> None:
        state["resets"].append(user)

    async def list_physical() -> list[str]:
        if state["physical"] is None:
            return sorted(fake_system.databases)
        return state["physical"]

    monkeypatch.setattr(mariadb, "reset_password", reset_password)
    monkeypatch.setattr(mariadb, "list_physical_databases", list_physical)
    return state


async def _active_site(admin_client, domain="db.example") -> int:
    resp = await admin_client.post("/api/sites", json={"domain": domain})
    assert resp.status_code == 202
    return resp.json()["site"]["id"]


async def test_create_database_returns_credentials_once(admin_client, fake_system, fake_mariadb):
    site_id = await _active_site(admin_client)
    resp = await admin_client.post(f"/api/databases/sites/{site_id}", json={"name": "shop_db"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["database"]["name"] == "shop_db"
    assert body["database"]["db_user"] == "shop_db"
    assert len(body["password"]) == 48
    assert "shop_db" in fake_system.databases

    # The password is nowhere in any later response.
    listing = (await admin_client.get("/api/databases")).json()
    assert body["password"] not in str(listing)


async def test_create_database_rejects_bad_names(admin_client, fake_system, fake_mariadb):
    site_id = await _active_site(admin_client)
    for bad in ["UPPER", "1abc", "with-dash", "a", "x" * 65, "name;drop"]:
        resp = await admin_client.post(f"/api/databases/sites/{site_id}", json={"name": bad})
        assert resp.status_code == 422, bad


async def test_create_duplicate_database_conflicts(admin_client, fake_system, fake_mariadb):
    site_id = await _active_site(admin_client)
    assert (
        await admin_client.post(f"/api/databases/sites/{site_id}", json={"name": "dup_db"})
    ).status_code == 201
    resp = await admin_client.post(f"/api/databases/sites/{site_id}", json={"name": "dup_db"})
    assert resp.status_code == 409


async def test_delete_database_requires_confirmation(admin_client, fake_system, fake_mariadb):
    site_id = await _active_site(admin_client)
    created = (
        await admin_client.post(f"/api/databases/sites/{site_id}", json={"name": "gone_db"})
    ).json()
    db_id = created["database"]["id"]

    resp = await admin_client.request(
        "DELETE", f"/api/databases/{db_id}", json={"confirm_name": "wrong"}
    )
    assert resp.status_code == 409

    resp = await admin_client.request(
        "DELETE", f"/api/databases/{db_id}", json={"confirm_name": "gone_db"}
    )
    assert resp.status_code == 204
    assert "gone_db" not in fake_system.databases


async def test_reset_password_returns_new_credentials(admin_client, fake_system, fake_mariadb):
    site_id = await _active_site(admin_client)
    created = (
        await admin_client.post(f"/api/databases/sites/{site_id}", json={"name": "rotate_db"})
    ).json()
    db_id = created["database"]["id"]

    resp = await admin_client.post(f"/api/databases/{db_id}/reset-password")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["password"]) == 48
    assert body["password"] != created["password"]
    assert fake_mariadb["resets"] == ["rotate_db"]


async def test_orphan_detection(admin_client, fake_system, fake_mariadb):
    site_id = await _active_site(admin_client)
    await admin_client.post(f"/api/databases/sites/{site_id}", json={"name": "known_db"})
    fake_mariadb["physical"] = ["known_db", "stray_db"]

    entries = (await admin_client.get("/api/databases")).json()
    known = next(e for e in entries if e["database"] and e["database"]["name"] == "known_db")
    assert known["missing"] is False
    assert known["site_domain"] == "db.example"
    orphan = next(e for e in entries if e["orphan_name"])
    assert orphan["orphan_name"] == "stray_db"

    # Panel record whose physical DB vanished → flagged missing.
    fake_mariadb["physical"] = ["stray_db"]
    entries = (await admin_client.get("/api/databases")).json()
    known = next(e for e in entries if e["database"])
    assert known["missing"] is True


async def test_delete_orphan_database(admin_client, fake_system, fake_mariadb, monkeypatch):
    dropped: list[str] = []

    async def drop_orphan(database: str) -> None:
        dropped.append(database)

    monkeypatch.setattr(mariadb, "drop_orphan_database", drop_orphan)
    site_id = await _active_site(admin_client)
    resp = await admin_client.post(f"/api/databases/sites/{site_id}", json={"name": "managed_db"})
    assert resp.status_code in (200, 201), resp.text
    fake_mariadb["physical"] = ["managed_db", "stray_db"]

    # Wrong confirmation rejected.
    resp = await admin_client.request(
        "DELETE", "/api/databases/orphans/stray_db", json={"confirm_name": "nope"}
    )
    assert resp.status_code == 409
    assert dropped == []

    # Managed databases are off limits here.
    resp = await admin_client.request(
        "DELETE", "/api/databases/orphans/managed_db", json={"confirm_name": "managed_db"}
    )
    assert resp.status_code == 409
    assert "managed by the panel" in resp.json()["error"]["message"]

    # System schemas and unknown names are rejected.
    resp = await admin_client.request(
        "DELETE", "/api/databases/orphans/mysql", json={"confirm_name": "mysql"}
    )
    assert resp.status_code == 409
    resp = await admin_client.request(
        "DELETE", "/api/databases/orphans/ghost_db", json={"confirm_name": "ghost_db"}
    )
    assert resp.status_code == 404

    # Happy path.
    resp = await admin_client.request(
        "DELETE", "/api/databases/orphans/stray_db", json={"confirm_name": "stray_db"}
    )
    assert resp.status_code == 204, resp.text
    assert dropped == ["stray_db"]


def test_drop_database_only_sql_is_scoped():
    sql = mariadb.build_drop_database_only_sql("stray_db")
    assert sql == "DROP DATABASE IF EXISTS `stray_db`;"
    with pytest.raises(mariadb.InvalidIdentifierError):
        mariadb.build_drop_database_only_sql("bad-name; DROP TABLE x")


async def test_databases_require_auth(client):
    assert (await client.get("/api/databases")).status_code == 401


# --- tokens & Adminer proxy ---------------------------------------------------------


def test_tokens_roundtrip_and_tamper():
    token = issue_token(secret="s3cret", ttl_seconds=60, scope="ticket")
    assert verify_token(token, secret="s3cret", scope="ticket")
    assert not verify_token(token, secret="s3cret", scope="session")  # wrong scope
    assert not verify_token(token, secret="other", scope="ticket")  # wrong key
    assert not verify_token(token + "x", secret="s3cret", scope="ticket")  # tampered
    expired = issue_token(secret="s3cret", ttl_seconds=-1, scope="ticket")
    assert not verify_token(expired, secret="s3cret", scope="ticket")


@pytest.fixture
def fake_adminer_upstream(app):
    """Wire a MockTransport client as the proxy's upstream."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<title>Adminer</title>")

    app.state.adminer_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


async def test_adminer_proxy_rejects_anonymous(client, fake_adminer_upstream):
    resp = await client.get("/adminer/")
    assert resp.status_code == 401
    assert fake_adminer_upstream == []


async def test_adminer_session_flow(admin_client, fake_adminer_upstream):
    # 1. Logged-in admin mints a ticket URL.
    resp = await admin_client.post("/api/databases/adminer-session")
    assert resp.status_code == 200
    url = resp.json()["url"]
    assert url.startswith("/adminer/?hosty_ticket=")

    # 2. Opening it proxies to Adminer and sets the scoped session cookie.
    resp = await admin_client.get(url)
    assert resp.status_code == 200
    assert "Adminer" in resp.text
    cookie = resp.headers.get("set-cookie", "")
    assert adminer_service.SESSION_COOKIE in cookie and "Path=/adminer" in cookie

    # 3. Subsequent requests ride the cookie; the ticket is stripped upstream.
    resp = await admin_client.get("/adminer/?server=localhost")
    assert resp.status_code == 200
    assert all("hosty_ticket" not in str(r.url) for r in fake_adminer_upstream)


async def test_adminer_proxy_rejects_garbage_ticket(client, fake_adminer_upstream):
    resp = await client.get("/adminer/?hosty_ticket=ticket.99999999999.deadbeef")
    assert resp.status_code == 401
