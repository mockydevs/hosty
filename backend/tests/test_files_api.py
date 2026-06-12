"""Filebrowser: API user management, scoped sessions, proxy identity enforcement."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.core.tickets import issue_token, token_scope
from app.services import filebrowser
from app.services.filebrowser import (
    AUTH_HEADER,
    build_config_init_argv,
    build_site_user_payload,
)
from app.system.users import InvalidSiteUserError
from tests.test_sites_api import FakeSystem

SITE_USER = "site-example-com-a1b2c3"


@pytest.fixture
def fb_settings() -> Settings:
    return Settings(_env_file=None)


# --- builders ---------------------------------------------------------------------


def test_config_init_argv(fb_settings):
    argv = build_config_init_argv(fb_settings)
    assert argv[:4] == ["filebrowser", "-d", "/var/lib/hosty/filebrowser.db", "config"]
    assert "--auth.method=proxy" in argv
    assert f"--auth.header={AUTH_HEADER}" in argv
    assert "--root=/var/www" in argv
    # Proxy auth auto-creates unknown users with the DEFAULT scope; it must
    # quarantine, never expose the sites root.
    assert f"--scope={filebrowser.QUARANTINE_SCOPE}" in argv
    assert "--baseurl=/files" in argv  # assets must resolve under the panel proxy
    assert "--address=127.0.0.1" in argv and "--port=8082" in argv


def test_site_user_payload_scopes_to_site_directory():
    payload = build_site_user_payload(SITE_USER, "example.com")
    assert payload["what"] == "user" and payload["which"] == []
    data = payload["data"]
    assert data["username"] == SITE_USER
    assert data["scope"] == "/example.com"
    assert data["lockPassword"] is True
    assert len(data["password"]) >= 24
    assert data["perm"]["admin"] is False and data["perm"]["execute"] is False
    assert data["perm"]["modify"] is True and data["perm"]["delete"] is True


def test_site_user_payload_rejects_bad_users_and_scopes():
    with pytest.raises(InvalidSiteUserError):
        build_site_user_payload("root", "example.com")
    for bad_domain in ["", ".", "..", "a/b", "x\x00y"]:
        with pytest.raises(filebrowser.FilebrowserError):
            build_site_user_payload(SITE_USER, bad_domain)


class FakeFilebrowserUpstream:
    """Mock of the Filebrowser REST API: /api/login, /api/users CRUD."""

    def __init__(self):
        self.users: list[dict] = [{"id": 1, "username": "admin", "scope": "/.hosty-quarantine"}]
        self._next_id = 2

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/login":
            if request.headers.get(AUTH_HEADER) != "admin":
                return httpx.Response(403)
            return httpx.Response(200, text="jwt-admin-token")
        if request.headers.get("X-Auth") != "jwt-admin-token":
            return httpx.Response(401)
        if request.url.path == "/api/users" and request.method == "GET":
            return httpx.Response(200, json=self.users)
        if request.url.path == "/api/users" and request.method == "POST":
            data = json.loads(request.content)["data"]
            self.users.append(
                {"id": self._next_id, "username": data["username"], "scope": data["scope"]}
            )
            self._next_id += 1
            return httpx.Response(201)
        if request.url.path.startswith("/api/users/") and request.method == "DELETE":
            uid = int(request.url.path.rsplit("/", 1)[1])
            self.users = [u for u in self.users if u["id"] != uid]
            return httpx.Response(200)
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def test_ensure_and_remove_site_user_roundtrip(fb_settings):
    upstream = FakeFilebrowserUpstream()
    async with upstream.client() as client:
        assert await filebrowser.ensure_site_user(
            SITE_USER, "example.com", fb_settings, client=client
        )
        assert any(u["username"] == SITE_USER for u in upstream.users)
        # Idempotent: second call is a no-op, not a duplicate.
        assert await filebrowser.ensure_site_user(
            SITE_USER, "example.com", fb_settings, client=client
        )
        assert sum(u["username"] == SITE_USER for u in upstream.users) == 1
        assert await filebrowser.remove_site_user(SITE_USER, fb_settings, client=client)
        assert not any(u["username"] == SITE_USER for u in upstream.users)
        assert not await filebrowser.remove_site_user(SITE_USER, fb_settings, client=client)


def test_token_scope_extraction_is_signature_bound():
    token = issue_token(secret="k", ttl_seconds=60, scope=f"files:{SITE_USER}")
    assert token_scope(token, secret="k") == f"files:{SITE_USER}"
    # Swapping the scope to another site invalidates the signature.
    other = token.replace(SITE_USER, "site-victim-b2c3d4")
    assert token_scope(other, secret="k") is None
    assert token_scope(token, secret="wrong") is None


# --- pipeline integration ----------------------------------------------------------


@pytest.fixture
def fake_system(monkeypatch) -> FakeSystem:
    fake = FakeSystem()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def fake_filebrowser(monkeypatch):
    state = {"users": set(), "fail": False}

    async def ensure(site_user: str, domain: str, settings) -> bool:
        if state["fail"]:
            raise filebrowser.FilebrowserError("boom")
        state["users"].add(site_user)
        return True

    async def remove(site_user: str, settings) -> bool:
        state["users"].discard(site_user)
        return True

    monkeypatch.setattr(filebrowser, "ensure_site_user", ensure)
    monkeypatch.setattr(filebrowser, "remove_site_user", remove)
    return state


async def test_create_site_registers_filebrowser_user(admin_client, fake_system, fake_filebrowser):
    resp = await admin_client.post("/api/sites", json={"domain": "fb.example"})
    op = (await admin_client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "succeeded"
    assert len(fake_filebrowser["users"]) == 1


async def test_filebrowser_failure_rolls_back_site(admin_client, fake_system, fake_filebrowser):
    fake_filebrowser["fail"] = True
    resp = await admin_client.post("/api/sites", json={"domain": "fbfail.example"})
    op = (await admin_client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "failed"
    statuses = {s["name"]: s["status"] for s in op["steps"]}
    assert statuses["filebrowser"] == "failed"
    assert statuses["caddy"] == "rolled_back"
    assert fake_system.linux_users == set()


async def test_unreachable_filebrowser_does_not_block_sites(admin_client, fake_system, monkeypatch):
    """Graceful degradation: filebrowser not installed/running → site still provisions."""

    def refuses(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        filebrowser,
        "_new_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuses)),
    )
    resp = await admin_client.post("/api/sites", json={"domain": "nofb.example"})
    op = (await admin_client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "succeeded"


# --- sessions & proxy ---------------------------------------------------------------


@pytest.fixture
def fake_files_upstream(app):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<title>File Browser</title>")

    app.state.files_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return seen


async def _site(admin_client, domain="files.example") -> int:
    resp = await admin_client.post("/api/sites", json={"domain": domain})
    return resp.json()["site"]["id"]


async def test_files_session_and_proxy_flow(
    admin_client, fake_system, fake_filebrowser, fake_files_upstream
):
    site_id = await _site(admin_client)
    resp = await admin_client.post(f"/api/sites/{site_id}/files-session")
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    assert url.startswith("/files/?hosty_ticket=")

    resp = await admin_client.get(url)
    assert resp.status_code == 200
    assert "File Browser" in resp.text
    cookie = resp.headers.get("set-cookie", "")
    assert "hosty_files" in cookie and "Path=/files" in cookie

    # Cookie rides subsequent requests; upstream always sees the auth header.
    resp = await admin_client.get("/files/api/resources/")
    assert resp.status_code == 200
    site_user = (await admin_client.get(f"/api/sites/{site_id}")).json()["site_user"]
    assert all(r.headers.get(AUTH_HEADER) == site_user for r in fake_files_upstream)
    # Filebrowser runs with baseurl=/files; the proxy must keep the prefix.
    assert all(r.url.path.startswith("/files") for r in fake_files_upstream)
    # Embedded in an iframe: must allow same-origin framing despite the
    # panel-wide X-Frame-Options: DENY default.
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    # Filebrowser bootstraps from an inline <script>; the strict panel-wide
    # CSP (default-src 'self') would block it and leave a blank iframe.
    assert "'unsafe-inline'" in resp.headers["content-security-policy"]


async def test_files_proxy_strips_spoofed_identity_header(
    admin_client, fake_system, fake_filebrowser, fake_files_upstream
):
    site_id = await _site(admin_client)
    url = (await admin_client.post(f"/api/sites/{site_id}/files-session")).json()["url"]
    site_user = (await admin_client.get(f"/api/sites/{site_id}")).json()["site_user"]

    resp = await admin_client.get(url, headers={AUTH_HEADER: "site-victim-b2c3d4"})
    assert resp.status_code == 200
    # The spoofed value never reaches Filebrowser — only the signed identity.
    assert fake_files_upstream[-1].headers[AUTH_HEADER] == site_user


async def test_files_proxy_rejects_anonymous_and_garbage(
    client, admin_client, fake_system, fake_filebrowser, fake_files_upstream
):
    assert (await client.get("/files/")).status_code == 401
    assert (await client.get("/files/?hosty_ticket=files-ticket:x.999.bad")).status_code == 401
    assert fake_files_upstream == []


async def test_files_session_requires_active_site(admin_client, fake_system, fake_filebrowser):
    assert (await admin_client.post("/api/sites/999/files-session")).status_code == 404
