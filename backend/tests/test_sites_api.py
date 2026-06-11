"""Sites API + provisioning pipeline (system layer fully faked).

Background tasks run before the ASGI transport returns, so operations are
already finished when we poll them — convenient for asserting end states.
"""

from __future__ import annotations

import pytest

from app.services import php_fpm
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
        self.pools: set[str] = set()
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

        def write_file(path: str, content: str, *, root: str) -> None:
            fake.files[path] = content

        async def chown_recursive(user: str, path: str, *, root: str) -> None:
            fake.calls.append(("chown", user, path))

        async def remove_tree(path: str, *, root: str) -> None:
            fake.calls.append(("remove_tree", path))

        async def install_pool(site_user: str, version: str, settings) -> None:
            fake._maybe_fail("php_pool")
            fake.calls.append(("install_pool", site_user, version))
            fake.pools.add(site_user)

        async def remove_pool(site_user: str, version: str, settings) -> bool:
            fake.calls.append(("remove_pool", site_user, version))
            fake.pools.discard(site_user)
            return True

        async def caddy_apply(self_client, config: dict) -> None:
            fake._maybe_fail("caddy")
            fake.calls.append(("caddy_apply", len(fake.caddy_configs)))
            fake.caddy_configs.append(config)

        monkeypatch.setattr(users, "create", user_create)
        monkeypatch.setattr(users, "delete", user_delete)
        monkeypatch.setattr(fs, "create_dir", create_dir)
        monkeypatch.setattr(fs, "write_file", write_file)
        monkeypatch.setattr(fs, "chown_recursive", chown_recursive)
        monkeypatch.setattr(fs, "remove_tree", remove_tree)
        monkeypatch.setattr(php_fpm, "install_pool", install_pool)
        monkeypatch.setattr(php_fpm, "remove_pool", remove_pool)
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
    assert [s["status"] for s in op["steps"]] == ["done"] * 5

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
