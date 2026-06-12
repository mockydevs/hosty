"""Sites API + provisioning pipeline (system layer fully faked).

Background tasks run before the ASGI transport returns, so operations are
already finished when we poll them — convenient for asserting end states.
"""

from __future__ import annotations

import pytest

from app.services import mariadb, php_fpm
from app.services.caddy import CaddyClient
from app.system import fs, users
from tests.conftest import setup_and_login  # noqa: F401  (fixture file)


class FakeSystem:
    """Records every mutation; can be told to fail a specific step."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.fail_on: str | None = None
        self.linux_users: set[str] = set()
        self.files: dict[str, str] = {}
        self.pools: set[tuple[str, str]] = set()
        self.databases: set[str] = set()
        self.caddy_configs: list[dict] = []

    def _maybe_fail(self, step: str) -> None:
        if self.fail_on == step:
            raise RuntimeError(f"injected failure in {step}")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        async def user_create(name: str) -> bool:
            fake._maybe_fail("linux_user")
            fake.calls.append(("user_create", name))
            fake.linux_users.add(name)
            return True

        async def user_delete(name: str) -> bool:
            fake.calls.append(("user_delete", name))
            fake.linux_users.discard(name)
            return True

        async def create_dir(path: str, *, root: str) -> None:
            fake._maybe_fail("doc_root")
            fake.calls.append(("create_dir", path))

        async def secure_site_layout(
            site_dir: str,
            doc_root: str,
            user: str,
            *,
            root: str,
            create: bool = True,
        ) -> None:
            fake._maybe_fail("doc_root")
            fake.calls.append(("secure_site_layout", site_dir, doc_root, user, create))

        def write_file(path: str, content: str, *, root: str) -> None:
            fake.files[path] = content

        async def chown_recursive(user: str, path: str, *, root: str) -> None:
            fake.calls.append(("chown", user, path))

        async def remove_tree(path: str, *, root: str) -> None:
            fake.calls.append(("remove_tree", path))

        async def install_pool(site_user: str, version: str, settings, **limits) -> None:
            fake._maybe_fail("php_pool")
            fake.calls.append(("install_pool", site_user, version, limits))
            fake.pools.add((site_user, version))

        async def remove_pool(site_user: str, version: str, settings) -> bool:
            fake.calls.append(("remove_pool", site_user, version))
            fake.pools.discard((site_user, version))
            return True

        async def create_database(database: str, user: str, password: str) -> None:
            fake._maybe_fail("database")
            fake.calls.append(("create_database", database, user))
            fake.databases.add(database)

        async def drop_database(database: str, user: str) -> None:
            fake.calls.append(("drop_database", database, user))
            fake.databases.discard(database)

        async def caddy_apply(self_client, config: dict) -> None:
            fake._maybe_fail("caddy")
            fake.calls.append(("caddy_apply", len(fake.caddy_configs)))
            fake.caddy_configs.append(config)

        monkeypatch.setattr(users, "create", user_create)
        monkeypatch.setattr(users, "delete", user_delete)
        monkeypatch.setattr(fs, "create_dir", create_dir)
        monkeypatch.setattr(fs, "secure_site_layout", secure_site_layout)
        monkeypatch.setattr(fs, "write_file", write_file)
        monkeypatch.setattr(fs, "chown_recursive", chown_recursive)
        monkeypatch.setattr(fs, "remove_tree", remove_tree)
        monkeypatch.setattr(php_fpm, "install_pool", install_pool)
        monkeypatch.setattr(php_fpm, "remove_pool", remove_pool)
        monkeypatch.setattr(mariadb, "create_database", create_database)
        monkeypatch.setattr(mariadb, "drop_database", drop_database)
        monkeypatch.setattr(CaddyClient, "apply", caddy_apply)


@pytest.fixture
def fake_system(monkeypatch) -> FakeSystem:
    fake = FakeSystem()
    fake.install(monkeypatch)
    return fake


async def test_create_site_happy_path(admin_client, fake_system):
    resp = await admin_client.post(
        "/api/sites", json={"domain": "Example.COM", "php_version": "8.3"}
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["site"]["domain"] == "example.com"
    assert body["site"]["status"] == "provisioning"
    site_id, op_id = body["site"]["id"], body["operation_id"]

    op = (await admin_client.get(f"/api/operations/{op_id}")).json()
    assert op["status"] == "succeeded"
    assert [s["status"] for s in op["steps"]] == ["done"] * 6

    site = (await admin_client.get(f"/api/sites/{site_id}")).json()
    assert site["status"] == "active"
    assert site["site_user"].startswith("site-")
    assert site["doc_root"] == "/var/www/example.com/public_html"

    # The skeleton index.php was written and the vhost published.
    assert any(p.endswith("/index.php") for p in fake_system.files)
    hosts = [
        r["match"][0]["host"][0]
        for r in fake_system.caddy_configs[-1]["apps"]["http"]["servers"]["hosty"]["routes"]
    ]
    assert hosts == ["example.com"]


async def test_create_site_failure_rolls_back_completed_steps(admin_client, fake_system):
    fake_system.fail_on = "caddy"
    resp = await admin_client.post("/api/sites", json={"domain": "fail.example"})
    assert resp.status_code == 202
    body = resp.json()

    op = (await admin_client.get(f"/api/operations/{body['operation_id']}")).json()
    assert op["status"] == "failed"
    assert "injected failure" in op["error"]
    statuses = {s["name"]: s["status"] for s in op["steps"]}
    assert statuses["caddy"] == "failed"
    assert statuses["linux_user"] == "rolled_back"
    assert statuses["doc_root"] == "rolled_back"
    assert statuses["php_pool"] == "rolled_back"
    assert statuses["finalize"] == "pending"

    # Compensations actually ran: user gone, pool gone, tree removed.
    assert fake_system.linux_users == set()
    assert fake_system.pools == set()
    assert any(c[0] == "remove_tree" for c in fake_system.calls)

    site = (await admin_client.get(f"/api/sites/{body['site']['id']}")).json()
    assert site["status"] == "error"
    assert "injected failure" in site["error_message"]


async def test_create_duplicate_domain_conflicts(admin_client, fake_system):
    first = await admin_client.post("/api/sites", json={"domain": "dup.example"})
    assert first.status_code == 202
    resp = await admin_client.post("/api/sites", json={"domain": "DUP.example"})
    assert resp.status_code == 409


async def test_create_site_rejects_invalid_domain(admin_client, fake_system):
    resp = await admin_client.post("/api/sites", json={"domain": "not_a_domain"})
    assert resp.status_code == 422


async def test_create_site_rejects_unknown_php_version(admin_client, fake_system):
    resp = await admin_client.post(
        "/api/sites", json={"domain": "ok.example", "php_version": "5.6"}
    )
    assert resp.status_code == 422


async def test_delete_requires_matching_confirmation(admin_client, fake_system):
    body = (await admin_client.post("/api/sites", json={"domain": "gone.example"})).json()
    site_id = body["site"]["id"]

    resp = await admin_client.request(
        "DELETE", f"/api/sites/{site_id}", json={"confirm_domain": "wrong.example"}
    )
    assert resp.status_code == 409

    resp = await admin_client.request(
        "DELETE", f"/api/sites/{site_id}", json={"confirm_domain": "GONE.example"}
    )
    assert resp.status_code == 202
    op = (await admin_client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "succeeded"

    assert (await admin_client.get(f"/api/sites/{site_id}")).status_code == 404
    assert fake_system.linux_users == set()
    assert fake_system.pools == set()
    # Final Caddy sync no longer serves the deleted domain.
    assert fake_system.caddy_configs[-1]["apps"]["http"]["servers"]["hosty"]["routes"] == []


async def test_sites_require_auth(client):
    assert (await client.get("/api/sites")).status_code == 401
    assert (await client.post("/api/sites", json={"domain": "x.example"})).status_code == 401


async def test_list_sites(admin_client, fake_system):
    for d in ["b.example", "a.example"]:
        await admin_client.post("/api/sites", json={"domain": d})
    rows = (await admin_client.get("/api/sites")).json()
    assert [r["domain"] for r in rows] == ["a.example", "b.example"]
    assert all(r["status"] == "active" for r in rows)


async def test_renew_ssl_resyncs_caddy_and_reprobes(admin_client, fake_system, monkeypatch):
    from app.services import ssl as ssl_service
    from app.services.ssl import CertStatus

    body = (await admin_client.post("/api/sites", json={"domain": "renew.example"})).json()
    site_id = body["site"]["id"]
    applies_before = len(fake_system.caddy_configs)

    async def fake_probe(domain: str, **kwargs) -> CertStatus:
        return CertStatus(domain=domain, status="active", issuer="Let's Encrypt")

    monkeypatch.setattr(ssl_service, "probe", fake_probe)

    resp = await admin_client.post(f"/api/sites/{site_id}/ssl/renew")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["domain"] == "renew.example"
    assert out["status"] == "active"
    assert out["issuer"] == "Let's Encrypt"
    # The full desired-state config was re-applied to Caddy.
    assert len(fake_system.caddy_configs) == applies_before + 1
    hosts = [
        r["match"][0]["host"][0]
        for r in fake_system.caddy_configs[-1]["apps"]["http"]["servers"]["hosty"]["routes"]
    ]
    assert "renew.example" in hosts


async def test_renew_ssl_caddy_failure_returns_502(admin_client, fake_system):
    body = (await admin_client.post("/api/sites", json={"domain": "broken.example"})).json()
    site_id = body["site"]["id"]

    fake_system.fail_on = "caddy"
    resp = await admin_client.post(f"/api/sites/{site_id}/ssl/renew")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "site_operation_failed"


async def test_renew_ssl_requires_active_site(admin_client, fake_system):
    assert (await admin_client.post("/api/sites/9999/ssl/renew")).status_code == 404


def _tls_policies(config: dict) -> list[dict]:
    return ((config.get("apps", {}).get("tls") or {}).get("automation") or {}).get("policies", [])


async def test_cloudflare_proxy_toggle_updates_caddy(admin_client, fake_system):
    body = (await admin_client.post("/api/sites", json={"domain": "proxied.example"})).json()
    site_id = body["site"]["id"]
    assert body["site"]["behind_cloudflare"] is False

    # Enable: the domain gets an internal-issuer TLS policy.
    resp = await admin_client.patch(
        f"/api/sites/{site_id}/cloudflare-proxy", json={"behind_cloudflare": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["behind_cloudflare"] is True
    policies = _tls_policies(fake_system.caddy_configs[-1])
    assert policies == [{"subjects": ["proxied.example"], "issuers": [{"module": "internal"}]}]

    # Toggling to the same value is a no-op (no extra Caddy apply).
    applies = len(fake_system.caddy_configs)
    resp = await admin_client.patch(
        f"/api/sites/{site_id}/cloudflare-proxy", json={"behind_cloudflare": True}
    )
    assert resp.status_code == 200
    assert len(fake_system.caddy_configs) == applies

    # Disable: the policy disappears again.
    resp = await admin_client.patch(
        f"/api/sites/{site_id}/cloudflare-proxy", json={"behind_cloudflare": False}
    )
    assert resp.status_code == 200
    assert resp.json()["behind_cloudflare"] is False
    assert _tls_policies(fake_system.caddy_configs[-1]) == []


async def test_cloudflare_proxy_toggle_rolls_back_on_caddy_failure(admin_client, fake_system):
    body = (await admin_client.post("/api/sites", json={"domain": "stuck.example"})).json()
    site_id = body["site"]["id"]

    fake_system.fail_on = "caddy"
    resp = await admin_client.patch(
        f"/api/sites/{site_id}/cloudflare-proxy", json={"behind_cloudflare": True}
    )
    assert resp.status_code == 502
    site = (await admin_client.get(f"/api/sites/{site_id}")).json()
    assert site["behind_cloudflare"] is False


async def test_ssl_status_uses_unverified_probe_for_proxied_site(
    admin_client, fake_system, monkeypatch
):
    from app.services import ssl as ssl_service
    from app.services.ssl import CertStatus

    body = (await admin_client.post("/api/sites", json={"domain": "edge.example"})).json()
    site_id = body["site"]["id"]
    await admin_client.patch(
        f"/api/sites/{site_id}/cloudflare-proxy", json={"behind_cloudflare": True}
    )

    seen: dict = {}

    async def fake_probe(domain: str, *, verify: bool = True, **kwargs) -> CertStatus:
        seen["verify"] = verify
        return CertStatus(domain=domain, status="origin_internal", issuer="Caddy internal CA")

    monkeypatch.setattr(ssl_service, "probe", fake_probe)
    resp = await admin_client.get(f"/api/sites/{site_id}/ssl")
    assert resp.status_code == 200
    assert resp.json()["status"] == "origin_internal"
    assert seen["verify"] is False


async def test_probe_unverified_reports_origin_internal(monkeypatch):
    from app.services import ssl as ssl_service

    class FakeWriter:
        def get_extra_info(self, key):
            return None

        def close(self):
            pass

    async def yes_dns(domain):
        return True

    async def fake_open_connection(*a, **k):
        return None, FakeWriter()

    monkeypatch.setattr(ssl_service, "_resolves", yes_dns)
    monkeypatch.setattr(ssl_service.asyncio, "open_connection", fake_open_connection)
    status = await ssl_service.probe("edge.example", verify=False)
    assert status.status == "origin_internal"
    assert status.issuer == "Caddy internal CA"
