"""Phase 11d account security: TOTP 2FA flow, active sessions, impersonation."""

from __future__ import annotations

from app.core import totp as totp_lib
from tests.conftest import TEST_PASSWORD, TEST_USER
from tests.test_multi_tenancy import CLIENT_PASSWORD, make_active_client

# --- 2FA enrollment + login -----------------------------------------------------------


async def _enroll(c, headers=None) -> str:
    """Run 2FA setup + enable for the authed client; returns the raw secret."""
    h = headers or {}
    resp = await c.post("/api/auth/2fa/setup", headers=h)
    assert resp.status_code == 200, resp.text
    secret = resp.json()["secret"]
    assert "otpauth://totp/" in resp.json()["otpauth_uri"]
    resp = await c.post(
        "/api/auth/2fa/enable",
        json={"code": totp_lib.totp_at(secret, __import__("time").time())},
        headers=h,
    )
    assert resp.status_code == 204, resp.text
    return secret


async def test_2fa_full_login_flow(admin_client, client):
    import time

    secret = await _enroll(admin_client)

    # /me reflects enrollment.
    me = (await admin_client.get("/api/auth/me")).json()
    assert me["totp_enabled"] is True

    # Password alone no longer yields tokens.
    resp = await client.post(
        "/api/auth/login",
        json={"username": TEST_USER, "password": TEST_PASSWORD},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["totp_required"] is True
    assert "access_token" not in body  # excluded when None
    challenge = body["challenge_token"]

    # Wrong code -> 401; correct code -> tokens.
    resp = await client.post(
        "/api/auth/login/totp",
        json={"challenge_token": challenge, "code": "000000"},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 401
    resp = await client.post(
        "/api/auth/login/totp",
        json={"challenge_token": challenge, "code": totp_lib.totp_at(secret, time.time())},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["access_token"]


async def test_2fa_challenge_token_is_not_an_access_token(admin_client, client):
    await _enroll(admin_client)
    resp = await client.post(
        "/api/auth/login",
        json={"username": TEST_USER, "password": TEST_PASSWORD},
        headers={"Authorization": ""},
    )
    challenge = resp.json()["challenge_token"]
    resp = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {challenge}"})
    assert resp.status_code == 401


async def test_2fa_garbage_challenge_rejected(admin_client, client):
    await _enroll(admin_client)
    resp = await client.post(
        "/api/auth/login/totp",
        json={"challenge_token": "garbage", "code": "123456"},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 401


async def test_2fa_disable_requires_password(admin_client):
    await _enroll(admin_client)
    resp = await admin_client.post("/api/auth/2fa/disable", json={"password": "wrong-password"})
    assert resp.status_code == 401
    resp = await admin_client.post("/api/auth/2fa/disable", json={"password": TEST_PASSWORD})
    assert resp.status_code == 204
    me = (await admin_client.get("/api/auth/me")).json()
    assert me["totp_enabled"] is False


async def test_2fa_enable_requires_setup_and_valid_code(admin_client):
    resp = await admin_client.post("/api/auth/2fa/enable", json={"code": "123456"})
    assert resp.status_code == 409  # no setup yet
    await admin_client.post("/api/auth/2fa/setup")
    resp = await admin_client.post("/api/auth/2fa/enable", json={"code": "000000"})
    assert resp.status_code == 401  # bad code
    me = (await admin_client.get("/api/auth/me")).json()
    assert me["totp_enabled"] is False  # never flipped on


async def test_2fa_setup_blocked_while_enabled(admin_client):
    await _enroll(admin_client)
    assert (await admin_client.post("/api/auth/2fa/setup")).status_code == 409


async def test_admin_can_reset_client_totp(admin_client, client):
    headers = await make_active_client(admin_client, client)
    await _enroll(client, headers=headers)

    users = (await admin_client.get("/api/users")).json()
    alice = next(u for u in users if u["username"] == "alice")
    assert alice["totp_enabled"] is True

    resp = await admin_client.patch(f"/api/users/{alice['id']}", json={"reset_totp": True})
    assert resp.status_code == 200
    assert resp.json()["totp_enabled"] is False

    # Password alone logs in again (no challenge step).
    resp = await client.post(
        "/api/auth/login",
        json={"username": "alice", "password": CLIENT_PASSWORD},
        headers={"Authorization": ""},
    )
    assert resp.json().get("totp_required", False) is False
    assert resp.json()["access_token"]


# --- active sessions ------------------------------------------------------------------


async def test_sessions_list_and_revoke(admin_client, client, app):
    # The admin_client login created one refresh session (cookie lives on `client`).
    resp = await admin_client.get("/api/auth/sessions")
    assert resp.status_code == 200
    sessions = resp.json()
    assert len(sessions) == 1 and sessions[0]["current"] is True

    # A second login = a second session, not "current" for this browser.
    resp = await client.post(
        "/api/auth/login",
        json={"username": TEST_USER, "password": TEST_PASSWORD},
        headers={"Authorization": ""},
    )
    assert resp.status_code == 200
    sessions = (await admin_client.get("/api/auth/sessions")).json()
    assert len(sessions) == 2

    other = next(s for s in sessions if not s["current"])
    assert (await admin_client.delete(f"/api/auth/sessions/{other['id']}")).status_code == 204
    sessions = (await admin_client.get("/api/auth/sessions")).json()
    assert [s["current"] for s in sessions] == [True]

    # Revoking an unknown/foreign session -> 404.
    assert (await admin_client.delete("/api/auth/sessions/9999")).status_code == 404


async def test_revoke_other_sessions(admin_client, client):
    for _ in range(2):
        await client.post(
            "/api/auth/login",
            json={"username": TEST_USER, "password": TEST_PASSWORD},
            headers={"Authorization": ""},
        )
    assert len((await admin_client.get("/api/auth/sessions")).json()) >= 2
    assert (await admin_client.post("/api/auth/sessions/revoke-others")).status_code == 204
    sessions = (await admin_client.get("/api/auth/sessions")).json()
    assert len(sessions) == 1 and sessions[0]["current"] is True


# --- impersonation --------------------------------------------------------------------


async def test_impersonation_flow(admin_client, client):
    headers = await make_active_client(admin_client, client)
    users = (await admin_client.get("/api/users")).json()
    alice = next(u for u in users if u["username"] == "alice")

    resp = await admin_client.post(f"/api/users/{alice['id']}/impersonate")
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    imp_headers = {"Authorization": f"Bearer {token}"}

    # Acting as the client, with the banner field set.
    me = (await client.get("/api/auth/me", headers=imp_headers)).json()
    assert me["username"] == "alice"
    assert me["impersonated_by"] == TEST_USER

    # The client's own view is unchanged.
    me = (await client.get("/api/auth/me", headers=headers)).json()
    assert me["impersonated_by"] is None

    # Admin-only surfaces stay closed to the impersonated session.
    assert (await client.get("/api/users", headers=imp_headers)).status_code == 403


async def test_impersonating_an_admin_is_refused(admin_client):
    me = (await admin_client.get("/api/auth/me")).json()
    assert (await admin_client.post(f"/api/users/{me['id']}/impersonate")).status_code == 409


async def test_clients_cannot_impersonate(admin_client, client):
    headers = await make_active_client(admin_client, client)
    users = (await admin_client.get("/api/users")).json()
    alice = next(u for u in users if u["username"] == "alice")
    resp = await client.post(f"/api/users/{alice['id']}/impersonate", headers=headers)
    assert resp.status_code == 403
