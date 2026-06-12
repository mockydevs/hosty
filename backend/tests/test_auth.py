from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient

from app.main import create_app
from tests.conftest import TEST_PASSWORD, TEST_USER, setup_and_login

NEW_PASSWORD = "an-even-longer-password-42"


async def test_setup_required_then_completed(client):
    assert (await client.get("/api/auth/setup")).json() == {"setup_required": True}
    resp = await client.post(
        "/api/auth/setup", json={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 201
    assert resp.json()["username"] == TEST_USER
    # Neither the password value nor any hash may leak (the boolean
    # `must_change_password` field is fine).
    assert TEST_PASSWORD not in resp.text
    assert "password_hash" not in resp.text
    assert "$argon2" not in resp.text
    assert (await client.get("/api/auth/setup")).json() == {"setup_required": False}


async def test_setup_locks_after_first_user(client):
    await setup_and_login(client)
    resp = await client.post("/api/auth/setup", json={"username": "intruder", "password": "x" * 20})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"


async def test_concurrent_setup_creates_exactly_one_admin(settings, tmp_path):
    db_path = (tmp_path / "setup-race.db").as_posix()
    application = create_app(
        settings.model_copy(update={"database_url": f"sqlite+aiosqlite:///{db_path}"})
    )
    async with application.router.lifespan_context(application):
        transport = ASGITransport(app=application)

        async def attempt(index: int) -> int:
            async with AsyncClient(transport=transport, base_url="http://testserver") as c:
                response = await c.post(
                    "/api/auth/setup",
                    json={"username": f"admin{index}", "password": TEST_PASSWORD},
                )
                return response.status_code

        statuses = await asyncio.gather(*(attempt(index) for index in range(8)))
        assert statuses.count(201) == 1
        assert statuses.count(409) == 7


async def test_setup_rejects_weak_password(client):
    resp = await client.post("/api/auth/setup", json={"username": TEST_USER, "password": "short"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


async def test_login_returns_token_and_refresh_cookie(client):
    await setup_and_login(client)
    resp = await client.post(
        "/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert client.cookies.get("hosty_refresh")
    set_cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=strict" in set_cookie


async def test_login_wrong_password(client):
    await setup_and_login(client)
    resp = await client.post(
        "/api/auth/login", json={"username": TEST_USER, "password": "wrong" * 4}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_login_rate_limited_after_repeated_failures(client, settings):
    await setup_and_login(client)  # successful login resets the window
    for _ in range(settings.login_rate_limit_attempts):
        resp = await client.post(
            "/api/auth/login", json={"username": TEST_USER, "password": "wrong-password-x"}
        )
        assert resp.status_code == 401
    resp = await client.post(
        "/api/auth/login", json={"username": TEST_USER, "password": "wrong-password-x"}
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limited"
    # Even the correct password is now blocked
    resp = await client.post(
        "/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 429


async def test_me_requires_auth(client):
    resp = await client.get("/api/auth/me")
    assert resp.status_code == 401


async def test_me_with_valid_token(admin_client):
    resp = await admin_client.get("/api/auth/me")
    assert resp.status_code == 200
    assert resp.json()["username"] == TEST_USER
    assert resp.json()["role"] == "admin"


async def test_garbage_token_rejected(client):
    resp = await client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"})
    assert resp.status_code == 401


async def test_refresh_rotates_token(client):
    await setup_and_login(client)
    first = await client.post("/api/auth/refresh")
    assert first.status_code == 200
    assert first.json()["access_token"]


async def test_refresh_reuse_revokes_session_family(client):
    await setup_and_login(client)
    old_cookie = client.cookies["hosty_refresh"]
    first = await client.post("/api/auth/refresh")
    assert first.status_code == 200
    new_cookie = client.cookies["hosty_refresh"]
    assert new_cookie != old_cookie

    # Present the OLD (rotated-out) token again -> theft detection
    client.cookies.clear()
    client.cookies.set("hosty_refresh", old_cookie, domain="testserver", path="/api/auth")
    reuse = await client.post("/api/auth/refresh")
    assert reuse.status_code == 401

    # The whole family is revoked: even the newest token is now dead
    client.cookies.clear()
    client.cookies.set("hosty_refresh", new_cookie, domain="testserver", path="/api/auth")
    after = await client.post("/api/auth/refresh")
    assert after.status_code == 401


async def test_concurrent_refresh_allows_only_one_rotation(client, app):
    await setup_and_login(client)
    old_cookie = client.cookies["hosty_refresh"]
    transport = ASGITransport(app=app)

    async def rotate() -> tuple[int, str | None]:
        async with AsyncClient(transport=transport, base_url="http://testserver") as c:
            response = await c.post(
                "/api/auth/refresh", headers={"Cookie": f"hosty_refresh={old_cookie}"}
            )
            return response.status_code, c.cookies.get("hosty_refresh")

    results = await asyncio.gather(rotate(), rotate())
    assert sorted(status for status, _cookie in results) == [200, 401]
    successor = next(cookie for status, cookie in results if status == 200)
    client.cookies.clear()
    client.cookies.set("hosty_refresh", successor, domain="testserver", path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_refresh_without_cookie(client):
    resp = await client.post("/api/auth/refresh")
    assert resp.status_code == 401


async def test_logout_revokes_refresh_token(client):
    await setup_and_login(client)
    raw_cookie = client.cookies["hosty_refresh"]
    resp = await client.post("/api/auth/logout")
    assert resp.status_code == 204
    # Re-present the revoked token explicitly
    client.cookies.clear()
    client.cookies.set("hosty_refresh", raw_cookie, domain="testserver", path="/api/auth")
    resp = await client.post("/api/auth/refresh")
    assert resp.status_code == 401


async def test_change_password_revokes_everything(client):
    token = await setup_and_login(client)
    auth = {"Authorization": f"Bearer {token}"}
    refresh_cookie = client.cookies["hosty_refresh"]

    resp = await client.post(
        "/api/auth/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": NEW_PASSWORD},
        headers=auth,
    )
    assert resp.status_code == 204

    # Old access token is dead
    assert (await client.get("/api/auth/me", headers=auth)).status_code == 401
    # Old refresh token is dead
    client.cookies.clear()
    client.cookies.set("hosty_refresh", refresh_cookie, domain="testserver", path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 401
    # Old password no longer works, new one does
    bad = await client.post(
        "/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert bad.status_code == 401
    good = await client.post(
        "/api/auth/login", json={"username": TEST_USER, "password": NEW_PASSWORD}
    )
    assert good.status_code == 200


async def test_change_password_requires_correct_current(admin_client):
    resp = await admin_client.post(
        "/api/auth/change-password",
        json={"current_password": "not-the-password", "new_password": NEW_PASSWORD},
    )
    assert resp.status_code == 401
