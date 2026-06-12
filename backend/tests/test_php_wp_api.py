"""PHP management + WordPress endpoints (system layer fully faked)."""

from __future__ import annotations

import pytest

from app.services import wordpress
from tests.test_sites_api import FakeSystem


@pytest.fixture
def fake_system(monkeypatch) -> FakeSystem:
    fake = FakeSystem()
    fake.install(monkeypatch)
    return fake


@pytest.fixture
def fake_wp(monkeypatch):
    """Fake WP-CLI: records calls; configurable failure + installed state."""

    class FakeWp:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.installed = False
            self.fail_on: str | None = None  # first arg to fail on, e.g. "config"

        def install(self, mp: pytest.MonkeyPatch) -> None:
            fake = self

            async def fake_run_wp(user, root, args, timeout=120.0, check=True):
                fake.calls.append(list(args))
                ok = True
                if args[:2] == ["core", "is-installed"]:
                    ok = fake.installed
                elif fake.fail_on and args[0] == fake.fail_on:
                    ok = False
                from app.system.runner import CommandResult

                result = CommandResult(
                    argv=("wp", *args),
                    returncode=0 if ok else 1,
                    stdout="",
                    stderr="boom" if not ok else "",
                    duration_ms=1.0,
                )
                if check and not ok:
                    raise wordpress.WordPressError(f"wp {args[0]} failed: boom")
                return result

            mp.setattr(wordpress, "run_wp", fake_run_wp)

    fake = FakeWp()
    fake.install(monkeypatch)
    return fake


async def _create_site(admin_client, domain="example.com") -> int:
    resp = await admin_client.post("/api/sites", json={"domain": domain})
    assert resp.status_code == 202
    return resp.json()["site"]["id"]


# --- PHP version switch -----------------------------------------------------------


async def test_change_php_version_zero_downtime_order(admin_client, fake_system):
    site_id = await _create_site(admin_client)
    fake_system.calls.clear()

    resp = await admin_client.post(f"/api/sites/{site_id}/php", json={"php_version": "8.4"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["php_version"] == "8.4"

    kinds = [c[0] for c in fake_system.calls]
    # New pool first, then Caddy repoint, then old pool removed — never a gap.
    assert kinds == ["install_pool", "caddy_apply", "remove_pool"]
    assert fake_system.calls[0][2] == "8.4"  # installed new version
    assert fake_system.calls[2][2] == "8.3"  # removed old version


async def test_change_php_version_failure_keeps_old_pool(admin_client, fake_system):
    site_id = await _create_site(admin_client)
    fake_system.calls.clear()
    fake_system.fail_on = "caddy"

    resp = await admin_client.post(f"/api/sites/{site_id}/php", json={"php_version": "8.4"})
    assert resp.status_code == 500 or resp.status_code == 502

    site = (await admin_client.get(f"/api/sites/{site_id}")).json()
    assert site["php_version"] == "8.3"  # unchanged
    kinds = [c[0] for c in fake_system.calls]
    assert kinds == ["install_pool", "remove_pool"]  # new pool cleaned up
    assert fake_system.calls[1][2] == "8.4"


async def test_change_php_version_rejects_unknown(admin_client, fake_system):
    site_id = await _create_site(admin_client)
    resp = await admin_client.post(f"/api/sites/{site_id}/php", json={"php_version": "7.4"})
    assert resp.status_code == 422


async def test_change_php_same_version_is_noop(admin_client, fake_system):
    site_id = await _create_site(admin_client)
    fake_system.calls.clear()
    resp = await admin_client.post(f"/api/sites/{site_id}/php", json={"php_version": "8.3"})
    assert resp.status_code == 200
    assert fake_system.calls == []


# --- PHP settings -----------------------------------------------------------------


async def test_update_php_settings(admin_client, fake_system):
    site_id = await _create_site(admin_client)
    fake_system.calls.clear()

    resp = await admin_client.patch(
        f"/api/sites/{site_id}/php-settings",
        json={"memory_limit": "512M", "upload_max_filesize": "128M"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["php_memory_limit"] == "512M"
    assert body["php_upload_max_filesize"] == "128M"
    install = next(c for c in fake_system.calls if c[0] == "install_pool")
    assert install[3] == {"memory_limit": "512M", "upload_max_filesize": "128M"}


async def test_update_php_settings_rejects_garbage(admin_client, fake_system):
    site_id = await _create_site(admin_client)
    for bad in ["0M", "1G", "512", "512M; rm -rf /", ""]:
        resp = await admin_client.patch(
            f"/api/sites/{site_id}/php-settings",
            json={"memory_limit": bad, "upload_max_filesize": "64M"},
        )
        assert resp.status_code == 422, bad


# --- WordPress --------------------------------------------------------------------


async def test_wp_status_not_installed(admin_client, fake_system, fake_wp):
    site_id = await _create_site(admin_client)
    resp = await admin_client.get(f"/api/sites/{site_id}/wordpress")
    assert resp.status_code == 200
    assert resp.json()["installed"] is False


WP_BODY = {
    "title": "My Blog",
    "admin_user": "admin",
    "admin_password": "a-strong-password",
    "admin_email": "don@example.com",
    "locale": "en_US",
    "version": "latest",
}


async def test_wp_install_happy_path(admin_client, fake_system, fake_wp):
    site_id = await _create_site(admin_client)
    resp = await admin_client.post(f"/api/sites/{site_id}/wordpress", json=WP_BODY)
    assert resp.status_code == 202, resp.text
    op_id = resp.json()["operation_id"]

    op = (await admin_client.get(f"/api/operations/{op_id}")).json()
    assert op["status"] == "succeeded", op
    assert [s["status"] for s in op["steps"]] == ["done"] * 5

    site = (await admin_client.get(f"/api/sites/{site_id}")).json()
    assert site["wordpress"] is True
    assert fake_system.databases == {"wp_example_com_" + site["site_user"].rsplit("-", 1)[-1]}

    wp_cmds = [c[:2] for c in fake_wp.calls]
    assert ["core", "download"] in wp_cmds
    assert ["config", "create"] in wp_cmds
    assert ["core", "install"] in wp_cmds


async def test_wp_install_failure_rolls_back_db_and_files(admin_client, fake_system, fake_wp):
    site_id = await _create_site(admin_client)
    fake_system.calls.clear()
    fake_wp.fail_on = "config"

    resp = await admin_client.post(f"/api/sites/{site_id}/wordpress", json=WP_BODY)
    op = (await admin_client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "failed"
    statuses = {s["name"]: s["status"] for s in op["steps"]}
    assert statuses["configure"] == "failed"
    assert statuses["database"] == "rolled_back"
    assert statuses["download"] == "rolled_back"

    assert fake_system.databases == set()  # DB dropped
    kinds = [c[0] for c in fake_system.calls]
    assert "remove_tree" in kinds and "create_dir" in kinds  # files restored to skeleton

    site = (await admin_client.get(f"/api/sites/{site_id}")).json()
    assert site["wordpress"] is False
    assert site["status"] == "active"  # the SITE itself is unharmed


async def test_wp_install_conflicts_when_already_installed(admin_client, fake_system, fake_wp):
    site_id = await _create_site(admin_client)
    assert (
        await admin_client.post(f"/api/sites/{site_id}/wordpress", json=WP_BODY)
    ).status_code == 202
    resp = await admin_client.post(f"/api/sites/{site_id}/wordpress", json=WP_BODY)
    assert resp.status_code == 409


async def test_wp_install_validates_fields(admin_client, fake_system, fake_wp):
    site_id = await _create_site(admin_client)
    for patch in [
        {"admin_email": "not-an-email"},
        {"locale": "en-US"},
        {"version": "six"},
        {"admin_password": "short"},
        {"title": ""},
    ]:
        resp = await admin_client.post(f"/api/sites/{site_id}/wordpress", json={**WP_BODY, **patch})
        assert resp.status_code == 422, patch


async def test_wp_action_requires_install(admin_client, fake_system, fake_wp):
    site_id = await _create_site(admin_client)
    resp = await admin_client.post(f"/api/sites/{site_id}/wordpress/actions/update_core")
    assert resp.status_code == 409


async def test_wp_actions_run(admin_client, fake_system, fake_wp):
    site_id = await _create_site(admin_client)
    await admin_client.post(f"/api/sites/{site_id}/wordpress", json=WP_BODY)
    fake_wp.calls.clear()

    resp = await admin_client.post(f"/api/sites/{site_id}/wordpress/actions/maintenance_on")
    assert resp.status_code == 200, resp.text
    assert ["maintenance-mode", "activate"] in [c[:2] for c in fake_wp.calls]

    resp = await admin_client.post(f"/api/sites/{site_id}/wordpress/actions/nonsense")
    assert resp.status_code == 404


async def test_wp_status_unhealthy_when_panel_installed_but_check_fails(
    admin_client, fake_system, fake_wp
):
    """A failing `wp core is-installed` on a panel-installed site must report
    "installed but unhealthy" — never "not installed" (which would offer the
    destructive install wizard for a live site)."""
    site_id = await _create_site(admin_client)
    fake_wp.installed = True
    resp = await admin_client.post(f"/api/sites/{site_id}/wordpress", json=WP_BODY)
    # Install rejected (already installed per the live check) — flip the flag
    # directly instead: simulate a panel-installed site whose check now fails.
    from app.db.models import Site
    from sqlalchemy import update as sa_update

    async with admin_client._transport.app.state.sessionmaker() as db:  # type: ignore[attr-defined]
        await db.execute(sa_update(Site).where(Site.id == site_id).values(wordpress=True))
        await db.commit()

    fake_wp.installed = False  # the live check now fails (e.g. DB down)
    body = (await admin_client.get(f"/api/sites/{site_id}/wordpress")).json()
    assert body["installed"] is True
    assert body["healthy"] is False
    assert body["detail"]  # carries the wp-cli error for diagnosis
