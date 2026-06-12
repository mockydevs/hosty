"""Panel domain / HTTPS API: runtime settings flip, env persistence, Caddy vhost."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from app.services import panel_config
from app.services.caddy import CaddyClient
from tests.conftest import setup_and_login


def test_update_env_file_set_replace_remove(tmp_path):
    path = str(tmp_path / "hosty.env")
    (tmp_path / "hosty.env").write_text("HOSTY_ENV=prod\nHOSTY_COOKIE_SECURE=true\n# a comment\n")

    panel_config.update_env_file(
        path,
        {
            "HOSTY_COOKIE_SECURE": "false",  # replace
            "HOSTY_PANEL_DOMAIN": "panel.example.com",  # append
        },
    )
    text = (tmp_path / "hosty.env").read_text()
    assert "HOSTY_COOKIE_SECURE=false" in text
    assert "HOSTY_PANEL_DOMAIN=panel.example.com" in text
    assert "HOSTY_ENV=prod" in text
    assert "# a comment" in text

    panel_config.update_env_file(path, {"HOSTY_PANEL_DOMAIN": None})  # remove
    text = (tmp_path / "hosty.env").read_text()
    assert "HOSTY_PANEL_DOMAIN" not in text
    assert "HOSTY_ENV=prod" in text


@pytest.fixture
def panel_settings(settings, tmp_path):
    return settings.model_copy(
        update={
            "env_file_path": str(tmp_path / "hosty.env"),
            "public_ip": "203.0.113.7",
        }
    )


@pytest_asyncio.fixture
async def panel_app(panel_settings, monkeypatch):
    applied: list[dict] = []

    async def fake_apply(self, config: dict) -> None:
        if getattr(fake_apply, "fail", False):
            raise RuntimeError("caddy down")
        applied.append(config)

    monkeypatch.setattr(CaddyClient, "apply", fake_apply)

    async def resolve_ok(domain: str) -> list[str]:
        return getattr(resolve_ok, "ips", ["203.0.113.7"])

    monkeypatch.setattr(panel_config, "resolve_ips", resolve_ok)

    application = create_app(panel_settings)
    async with application.router.lifespan_context(application):
        application.state.caddy_applied = applied
        application.state.fake_apply = fake_apply
        application.state.fake_resolve = resolve_ok
        yield application


@pytest_asyncio.fixture
async def panel_client(panel_app):
    transport = ASGITransport(app=panel_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        token = await setup_and_login(c)
        c.headers["Authorization"] = f"Bearer {token}"
        yield c


async def test_panel_domain_requires_auth(panel_app):
    transport = ASGITransport(app=panel_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        assert (await c.get("/api/system/panel-domain")).status_code == 401


async def test_set_and_clear_panel_domain(panel_client, panel_app, panel_settings, tmp_path):
    # Default: nothing configured.
    resp = await panel_client.get("/api/system/panel-domain")
    assert resp.json() == {"domain": None, "cookie_secure": False, "url": None}

    # Enable.
    resp = await panel_client.put("/api/system/panel-domain", json={"domain": "Panel.Example.COM"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "domain": "panel.example.com",
        "cookie_secure": True,
        "url": "https://panel.example.com",
    }

    # Caddy got the panel vhost.
    routes = panel_app.state.caddy_applied[-1]["apps"]["http"]["servers"]["hosty"]["routes"]
    assert routes[0]["match"][0]["host"] == ["panel.example.com"]

    # Persisted to the env file.
    text = (tmp_path / "hosty.env").read_text()
    assert "HOSTY_PANEL_DOMAIN=panel.example.com" in text
    assert "HOSTY_COOKIE_SECURE=true" in text

    # The refresh cookie is now Secure.
    assert panel_settings.cookie_secure is True

    # Disable again.
    resp = await panel_client.delete("/api/system/panel-domain")
    assert resp.status_code == 200
    assert resp.json() == {"domain": None, "cookie_secure": False, "url": None}
    text = (tmp_path / "hosty.env").read_text()
    assert "HOSTY_PANEL_DOMAIN" not in text
    assert "HOSTY_COOKIE_SECURE=false" in text

    assert (await panel_client.delete("/api/system/panel-domain")).status_code == 404


async def test_set_panel_domain_dns_checks(panel_client, panel_app):
    # Resolves elsewhere -> rejected with a clear message.
    panel_app.state.fake_resolve.ips = ["198.51.100.9"]
    resp = await panel_client.put("/api/system/panel-domain", json={"domain": "p.example.com"})
    assert resp.status_code == 409
    assert "resolves to 198.51.100.9" in resp.json()["error"]["message"]

    # Does not resolve at all -> rejected.
    panel_app.state.fake_resolve.ips = []
    resp = await panel_client.put("/api/system/panel-domain", json={"domain": "p.example.com"})
    assert resp.status_code == 409
    assert "does not resolve" in resp.json()["error"]["message"]

    # force=true skips the check.
    resp = await panel_client.put(
        "/api/system/panel-domain", json={"domain": "p.example.com", "force": True}
    )
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://p.example.com"


async def test_set_panel_domain_caddy_failure_rolls_back(
    panel_client, panel_app, panel_settings, tmp_path
):
    panel_app.state.fake_apply.fail = True
    resp = await panel_client.put("/api/system/panel-domain", json={"domain": "p.example.com"})
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "panel_domain_failed"
    assert panel_settings.panel_domain is None
    assert panel_settings.cookie_secure is False
    assert not (tmp_path / "hosty.env").exists()


async def test_set_panel_domain_rejects_invalid_domain(panel_client):
    resp = await panel_client.put("/api/system/panel-domain", json={"domain": "not a domain"})
    assert resp.status_code == 409


# --- one-click DNS record ---------------------------------------------------------------


async def test_panel_domain_one_click_dns_record(panel_client, panel_app, panel_settings):
    from tests.test_dns_api import FakePDNS

    fake = FakePDNS()
    await fake.create_zone("mailer.co.ke.", ["ns1.mailer.co.ke."])
    await fake.create_zone("co.ke.", ["ns1.co.ke."])  # shorter match must lose
    panel_app.state.pdns_client = fake

    resp = await panel_client.post(
        "/api/system/panel-domain/dns-record", json={"domain": "panel.mailer.co.ke"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "zone": "mailer.co.ke",
        "name": "panel.mailer.co.ke",
        "type": "A",
        "content": "203.0.113.7",
        "ttl": panel_settings.dns_default_ttl,
    }
    # The record landed in the most specific hosted zone.
    zone = await fake.get_zone("mailer.co.ke.")
    records = {(r["name"], r["type"]): r["records"] for r in zone["rrsets"]}
    assert records[("panel.mailer.co.ke.", "A")] == [{"content": "203.0.113.7", "disabled": False}]


async def test_panel_domain_dns_record_requires_matching_zone(panel_client, panel_app):
    from tests.test_dns_api import FakePDNS

    panel_app.state.pdns_client = FakePDNS()
    resp = await panel_client.post(
        "/api/system/panel-domain/dns-record", json={"domain": "panel.example.com"}
    )
    assert resp.status_code == 409
    assert "No zone on the DNS page" in resp.json()["error"]["message"]


async def test_panel_domain_dns_record_is_admin_only(panel_app):
    from tests.test_dns_api import FakePDNS

    panel_app.state.pdns_client = FakePDNS()
    transport = ASGITransport(app=panel_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as anon:
        resp = await anon.post(
            "/api/system/panel-domain/dns-record", json={"domain": "panel.example.com"}
        )
        assert resp.status_code == 401
