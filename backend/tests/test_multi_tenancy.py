"""Phase 11a: users API, owner scoping (404 for other tenants), quotas,
suspension, and the forced temp-password change flow."""

from __future__ import annotations

import pytest_asyncio

from app.services import mail as mail_service
from tests.test_sites_api import FakeSystem

CLIENT_PASSWORD = "client-secret-password-1"


@pytest_asyncio.fixture
async def fake_system(monkeypatch) -> FakeSystem:
    fake = FakeSystem()
    fake.install(monkeypatch)
    return fake


async def create_client_user(admin_client, username: str = "alice", **kwargs) -> dict:
    resp = await admin_client.post("/api/users", json={"username": username, **kwargs})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def login_as(client, username: str, password: str) -> dict:
    """Headers dict for `username`; does not touch the client's default headers."""
    resp = await client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def make_active_client(admin_client, client, username: str = "alice", **kwargs) -> dict:
    """Create a client, complete the forced password change, return headers."""
    created = await create_client_user(admin_client, username, **kwargs)
    temp_headers = await login_as(client, username, created["temp_password"])
    resp = await client.post(
        "/api/auth/change-password",
        json={"current_password": created["temp_password"], "new_password": CLIENT_PASSWORD},
        headers=temp_headers,
    )
    assert resp.status_code == 204, resp.text
    return await login_as(client, username, CLIENT_PASSWORD)


# --- users API -------------------------------------------------------------------


async def test_users_api_is_admin_only(admin_client, client):
    headers = await make_active_client(admin_client, client)
    resp = await client.get("/api/users", headers=headers)
    assert resp.status_code == 403
    resp = await client.post("/api/users", json={"username": "bob"}, headers=headers)
    assert resp.status_code == 403


async def test_create_client_with_generated_temp_password(admin_client):
    created = await create_client_user(admin_client, "alice", max_sites=2, max_databases=3)
    assert created["user"]["role"] == "client"
    assert created["user"]["email"] is None
    assert created["user"]["phone"] is None
    assert created["user"]["must_change_password"] is True
    assert created["user"]["max_sites"] == 2
    assert len(created["temp_password"]) >= 12

    listed = (await admin_client.get("/api/users")).json()
    assert {u["username"] for u in listed} == {"admin", "alice"}


async def test_duplicate_username_conflicts(admin_client):
    await create_client_user(admin_client, "alice")
    resp = await admin_client.post("/api/users", json={"username": "alice"})
    assert resp.status_code == 409


async def test_create_client_emails_temp_password_when_smtp_configured(admin_client, monkeypatch):
    sent = []

    async def fake_send(config, *, to, subject, text):
        sent.append((to, subject, text))

    monkeypatch.setattr(mail_service, "send", fake_send)
    await admin_client.put(
        "/api/notifications/smtp",
        json={
            "host": "smtp.example.com",
            "port": 587,
            "from_email": "panel@example.com",
            "security": "starttls",
            "notification_recipients": [],
        },
    )

    created = await create_client_user(
        admin_client, "alice", email="Alice@Example.com", phone=" +1 555 0100 "
    )
    assert created["user"]["email"] == "alice@example.com"
    assert created["user"]["phone"] == "+1 555 0100"
    assert created["email_sent"] is True
    assert created["temp_password"] is None
    assert sent[0][0] == "alice@example.com"
    assert "temporary password" in sent[0][1].lower()
    assert "alice" in sent[0][2]


async def test_temp_password_forces_change(admin_client, client):
    created = await create_client_user(admin_client, "alice")
    headers = await login_as(client, "alice", created["temp_password"])

    # Everything except /me, change-password, logout is blocked.
    resp = await client.get("/api/sites", headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "password_change_required"
    assert (await client.get("/api/auth/me", headers=headers)).status_code == 200

    resp = await client.post(
        "/api/auth/change-password",
        json={"current_password": created["temp_password"], "new_password": CLIENT_PASSWORD},
        headers=headers,
    )
    assert resp.status_code == 204
    headers = await login_as(client, "alice", CLIENT_PASSWORD)
    me = (await client.get("/api/auth/me", headers=headers)).json()
    assert me["must_change_password"] is False
    assert (await client.get("/api/sites", headers=headers)).status_code == 200


async def test_suspend_blocks_login_and_existing_tokens(admin_client, client):
    headers = await make_active_client(admin_client, client)
    user_id = next(
        u["id"] for u in (await admin_client.get("/api/users")).json() if u["username"] == "alice"
    )

    resp = await admin_client.patch(f"/api/users/{user_id}", json={"suspended": True})
    assert resp.status_code == 200 and resp.json()["suspended"] is True

    # Existing access token dies immediately; new logins are rejected.
    resp = await client.get("/api/sites", headers=headers)
    assert resp.status_code == 401
    resp = await client.post(
        "/api/auth/login",
        json={"username": "alice", "password": CLIENT_PASSWORD},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 401 and "suspended" in resp.text

    # Unsuspend: login works again.
    await admin_client.patch(f"/api/users/{user_id}", json={"suspended": False})
    await login_as(client, "alice", CLIENT_PASSWORD)


async def test_admin_account_cannot_be_suspended_or_deleted(admin_client):
    admin_id = (await admin_client.get("/api/auth/me")).json()["id"]
    resp = await admin_client.patch(f"/api/users/{admin_id}", json={"suspended": True})
    assert resp.status_code == 409
    resp = await admin_client.request(
        "DELETE", f"/api/users/{admin_id}", json={"mode": "reassign", "confirm_username": "admin"}
    )
    assert resp.status_code == 409


async def test_reset_password_issues_new_temp(admin_client, client):
    await make_active_client(admin_client, client)
    user_id = next(
        u["id"] for u in (await admin_client.get("/api/users")).json() if u["username"] == "alice"
    )
    resp = await admin_client.post(f"/api/users/{user_id}/reset-password")
    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["must_change_password"] is True
    # Old password no longer works; the new temp one does.
    bad = await client.post(
        "/api/auth/login",
        json={"username": "alice", "password": CLIENT_PASSWORD},
        headers={"Authorization": ""},
    )
    assert bad.status_code == 401
    await login_as(client, "alice", body["temp_password"])


# --- ownership scoping --------------------------------------------------------------


async def test_clients_only_see_their_own_sites(admin_client, client, fake_system):
    resp = await admin_client.post("/api/sites", json={"domain": "admin-site.example"})
    assert resp.status_code == 202
    admin_site_id = resp.json()["site"]["id"]

    headers = await make_active_client(admin_client, client)
    resp = await client.post("/api/sites", json={"domain": "client-site.example"}, headers=headers)
    assert resp.status_code == 202
    client_site_id = resp.json()["site"]["id"]

    # List is filtered; the other tenant's site 404s (no existence leak).
    domains = [s["domain"] for s in (await client.get("/api/sites", headers=headers)).json()]
    assert domains == ["client-site.example"]
    assert (await client.get(f"/api/sites/{admin_site_id}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/sites/{client_site_id}", headers=headers)).status_code == 200

    # Admin sees everything.
    domains = [s["domain"] for s in (await admin_client.get("/api/sites")).json()]
    assert domains == ["admin-site.example", "client-site.example"]


async def test_client_cannot_touch_other_tenants_site(admin_client, client, fake_system):
    resp = await admin_client.post("/api/sites", json={"domain": "admin-site.example"})
    site_id = resp.json()["site"]["id"]
    headers = await make_active_client(admin_client, client)

    delete = await client.request(
        "DELETE",
        f"/api/sites/{site_id}",
        json={"confirm_domain": "admin-site.example"},
        headers=headers,
    )
    assert delete.status_code == 404
    assert (
        await client.post(f"/api/sites/{site_id}/php", json={"php_version": "8.4"}, headers=headers)
    ).status_code == 404
    assert (await client.get(f"/api/sites/{site_id}/backups", headers=headers)).status_code == 404
    assert (
        await client.post(
            f"/api/databases/sites/{site_id}", json={"name": "sneaky_db"}, headers=headers
        )
    ).status_code == 404


async def test_operations_scoped_to_owner(admin_client, client, fake_system):
    resp = await admin_client.post("/api/sites", json={"domain": "admin-site.example"})
    op_id = resp.json()["operation_id"]
    headers = await make_active_client(admin_client, client)
    assert (await client.get(f"/api/operations/{op_id}", headers=headers)).status_code == 404
    assert (await admin_client.get(f"/api/operations/{op_id}")).status_code == 200
    # The list endpoint is filtered the same way.
    assert (await client.get("/api/operations", headers=headers)).json() == []
    assert len((await admin_client.get("/api/operations")).json()) == 1


async def test_audit_log_scoped_to_own_entries(admin_client, client, fake_system):
    await admin_client.post("/api/sites", json={"domain": "admin-site.example"})
    headers = await make_active_client(admin_client, client)
    await client.post("/api/sites", json={"domain": "client-site.example"}, headers=headers)

    page = (await client.get("/api/audit", headers=headers)).json()
    assert page["total"] > 0
    assert all(e["username"] == "alice" for e in page["entries"])

    admin_page = (await admin_client.get("/api/audit")).json()
    assert {e["username"] for e in admin_page["entries"]} >= {"admin", "alice"}


# --- quotas ---------------------------------------------------------------------------


async def test_site_quota_enforced(admin_client, client, fake_system):
    headers = await make_active_client(admin_client, client, max_sites=1)
    first = await client.post("/api/sites", json={"domain": "one.example"}, headers=headers)
    assert first.status_code == 202
    second = await client.post("/api/sites", json={"domain": "two.example"}, headers=headers)
    assert second.status_code == 409
    assert "quota" in second.text


async def test_database_quota_enforced(admin_client, client, fake_system):
    headers = await make_active_client(admin_client, client, max_databases=1)
    resp = await client.post("/api/sites", json={"domain": "one.example"}, headers=headers)
    site_id = resp.json()["site"]["id"]
    first = await client.post(
        f"/api/databases/sites/{site_id}", json={"name": "db_one"}, headers=headers
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        f"/api/databases/sites/{site_id}", json={"name": "db_two"}, headers=headers
    )
    assert second.status_code == 409
    assert "quota" in second.text


async def test_quota_can_be_updated_and_cleared(admin_client, client):
    created = await create_client_user(admin_client, "alice", max_sites=1)
    user_id = created["user"]["id"]
    resp = await admin_client.patch(f"/api/users/{user_id}", json={"max_sites": 5})
    assert resp.json()["max_sites"] == 5
    resp = await admin_client.patch(f"/api/users/{user_id}", json={"clear_max_sites": True})
    assert resp.json()["max_sites"] is None


# --- delete user ---------------------------------------------------------------------


async def test_delete_user_reassigns_sites_to_admin(admin_client, client, fake_system):
    headers = await make_active_client(admin_client, client)
    await client.post("/api/sites", json={"domain": "client-site.example"}, headers=headers)
    user_id = next(
        u["id"] for u in (await admin_client.get("/api/users")).json() if u["username"] == "alice"
    )

    wrong = await admin_client.request(
        "DELETE", f"/api/users/{user_id}", json={"mode": "reassign", "confirm_username": "nope"}
    )
    assert wrong.status_code == 409

    resp = await admin_client.request(
        "DELETE", f"/api/users/{user_id}", json={"mode": "reassign", "confirm_username": "alice"}
    )
    assert resp.status_code == 202
    assert resp.json()["reassigned_sites"] == 1

    # The site survives under the admin; the client account is gone.
    domains = [s["domain"] for s in (await admin_client.get("/api/sites")).json()]
    assert domains == ["client-site.example"]
    assert (await client.get("/api/sites", headers=headers)).status_code == 401


async def test_delete_user_with_site_teardown(admin_client, client, fake_system):
    headers = await make_active_client(admin_client, client)
    await client.post("/api/sites", json={"domain": "client-site.example"}, headers=headers)
    user_id = next(
        u["id"] for u in (await admin_client.get("/api/users")).json() if u["username"] == "alice"
    )

    resp = await admin_client.request(
        "DELETE",
        f"/api/users/{user_id}",
        json={"mode": "delete_sites", "confirm_username": "alice"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert len(body["operation_ids"]) == 1

    op = (await admin_client.get(f"/api/operations/{body['operation_ids'][0]}")).json()
    assert op["status"] == "succeeded"
    assert (await admin_client.get("/api/sites")).json() == []


# --- admin-only surfaces --------------------------------------------------------------


async def test_admin_only_surfaces_are_forbidden_for_clients(admin_client, client, app):
    from tests.test_dns_api import FakePDNS

    app.state.pdns_client = FakePDNS()
    headers = await make_active_client(admin_client, client)
    # Phase 11b: DNS is tenant-scoped (no longer admin-only) — a fresh client
    # gets an empty zone list, never other tenants' zones.
    resp = await client.get("/api/dns/zones", headers=headers)
    assert resp.status_code == 200 and resp.json() == []
    assert (await client.get("/api/dns/meta", headers=headers)).status_code == 200
    assert (await client.get("/api/backups/s3-config", headers=headers)).status_code == 403
    assert (await client.post("/api/databases/adminer-session", headers=headers)).status_code == 403
    assert (
        await client.post("/api/system/services/caddy/actions/restart", headers=headers)
    ).status_code == 403
    assert (
        await client.put("/api/system/panel-domain", json={"domain": "x.example"}, headers=headers)
    ).status_code == 403
    # Read-only system surfaces stay available.
    assert (await client.get("/api/system/stats", headers=headers)).status_code == 200
