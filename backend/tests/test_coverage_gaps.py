"""Targeted tests for execution paths the API-level suites do not reach
(Week 23 coverage gate: >=85% on app/system + app/services)."""

from __future__ import annotations

import ssl as ssl_module

import pytest

from app.core.config import Settings
from app.services import mariadb, php_fpm, ssl, wordpress
from app.system import fs
from app.system.runner import CommandResult


def _result(ok: bool = True, stdout: str = "", stderr: str = "") -> CommandResult:
    return CommandResult(("x",), 0 if ok else 1, stdout, stderr, 1.0)


@pytest.fixture
def settings() -> Settings:
    return Settings(caddy_sync_on_startup=False, _env_file=None)


# --- services/ssl.py ---------------------------------------------------------------


def test_parse_peer_cert_full():
    cert = {
        "issuer": ((("organizationName", "Let's Encrypt"),), (("commonName", "R11"),)),
        "notAfter": "Sep  9 12:00:00 2026 GMT",
    }
    status = ssl.parse_peer_cert("example.com", cert)
    assert status.status == "active"
    assert status.issuer == "Let's Encrypt"
    assert status.not_after is not None and status.not_after.year == 2026


def test_parse_peer_cert_empty():
    status = ssl.parse_peer_cert("example.com", {})
    assert status.status == "active" and status.issuer is None and status.not_after is None


async def test_probe_dns_unresolved(monkeypatch):
    async def no_dns(domain):
        return False

    monkeypatch.setattr(ssl, "_resolves", no_dns)
    status = await ssl.probe("nope.example")
    assert status.status == "dns_unresolved"
    assert "does not resolve" in status.detail


async def test_probe_handshake_failure(monkeypatch):
    async def yes_dns(domain):
        return True

    async def boom(*a, **k):
        raise ssl_module.SSLError("handshake failed")

    monkeypatch.setattr(ssl, "_resolves", yes_dns)
    monkeypatch.setattr(ssl.asyncio, "open_connection", boom)
    status = await ssl.probe("failing.example")
    assert status.status == "no_certificate"
    assert "SSLError" in status.detail


async def test_resolves_real_loopback():
    assert await ssl._resolves("localhost") is True
    assert await ssl._resolves("definitely-not-a-real-host.invalid") is False


# --- system/fs.py ------------------------------------------------------------------


@pytest.fixture
def fake_runner(monkeypatch):
    state = {"ok": True, "calls": []}

    async def run(argv, **kw):
        state["calls"].append(list(argv))
        return _result(ok=state["ok"], stderr="" if state["ok"] else "boom")

    monkeypatch.setattr("app.system.runner.run", run)
    return state


async def test_fs_operations_run_expected_argv(fake_runner):
    await fs.create_dir("/var/www/a.com", root="/var/www")
    await fs.chown_recursive("site-a-abc123", "/var/www/a.com", root="/var/www")
    await fs.remove_tree("/var/www/a.com", root="/var/www")
    assert [c[0] for c in fake_runner["calls"]] == ["mkdir", "chown", "rm"]


async def test_fs_operations_raise_on_failure(fake_runner):
    fake_runner["ok"] = False
    with pytest.raises(fs.FsOperationError, match="mkdir"):
        await fs.create_dir("/var/www/a.com", root="/var/www")


def test_fs_write_file(tmp_path):
    target = tmp_path / "site" / "index.php"
    target.parent.mkdir()
    fs.write_file(str(target), "<?php", root=str(tmp_path))
    assert target.read_text() == "<?php"


# --- services/php_fpm.py -----------------------------------------------------------


@pytest.fixture
def pool_env(tmp_path, settings, monkeypatch):
    s = settings.model_copy(
        update={"php_pool_dir_template": str(tmp_path) + "/php/{version}/pool.d"}
    )
    (tmp_path / "php" / "8.3" / "pool.d").mkdir(parents=True)
    reloads: list[tuple[str, str]] = []

    async def fake_control(action, unit):
        reloads.append((action, unit))

    monkeypatch.setattr("app.services.php_fpm.systemd.control", fake_control)
    return s, reloads


async def test_install_and_remove_pool_lifecycle(pool_env):
    s, reloads = pool_env
    await php_fpm.install_pool(
        "site-a-abc123", "8.3", s, doc_root="/var/www/a.example/public_html"
    )
    path = php_fpm.pool_file_path("site-a-abc123", "8.3", s)
    with open(path) as fh:
        assert "[site-a-abc123]" in fh.read()
    assert reloads == [("reload", "php8.3-fpm")]

    assert await php_fpm.remove_pool("site-a-abc123", "8.3", s) is True
    assert await php_fpm.remove_pool("site-a-abc123", "8.3", s) is False  # idempotent
    assert len(reloads) == 2  # second remove did not reload


# --- services/mariadb.py -----------------------------------------------------------


async def test_mariadb_execute_paths(fake_runner):
    await mariadb.create_database("wp_ok", "wp_ok", "a" * 16)
    await mariadb.drop_database("wp_ok", "wp_ok")
    await mariadb.reset_password("wp_ok", "b" * 16)
    assert all(c[0] == "mariadb" for c in fake_runner["calls"])

    fake_runner["ok"] = False
    with pytest.raises(mariadb.MariaDBError):
        await mariadb.create_database("wp_ok", "wp_ok", "a" * 16)
    with pytest.raises(mariadb.MariaDBError):
        await mariadb.list_physical_databases()


async def test_list_physical_databases_filters_system_schemas(monkeypatch):
    async def run(argv, **kw):
        return _result(stdout="information_schema\nmysql\nwp_site\nsys\nshop_db\n")

    monkeypatch.setattr("app.system.runner.run", run)
    assert await mariadb.list_physical_databases() == ["wp_site", "shop_db"]


# --- services/wordpress.py ---------------------------------------------------------


async def test_run_wp_raises_on_failure(monkeypatch):
    async def run(argv, **kw):
        return _result(ok=False, stderr="Error: something exploded")

    monkeypatch.setattr("app.system.runner.run", run)
    with pytest.raises(wordpress.WordPressError, match="exploded"):
        await wordpress.run_wp("site-a-abc123", "/var/www/a/public_html", ["core", "version"])


async def test_run_wp_sets_cache_env(monkeypatch):
    seen = {}

    async def run(argv, timeout=30.0, env=None, **kw):
        seen["env"] = env
        return _result()

    monkeypatch.setattr("app.system.runner.run", run)
    await wordpress.run_wp("site-a-abc123", "/var/www/a/public_html", ["core", "version"])
    assert seen["env"]["WP_CLI_CACHE_DIR"] == wordpress.WP_CLI_CACHE_DIR
    assert seen["env"]["HOME"] == "/home/site-a-abc123"


class FakeSite:
    site_user = "site-a-abc123"
    doc_root = "/var/www/a/public_html"


async def test_wp_actions_maintenance_and_salts(monkeypatch):
    calls: list[list[str]] = []

    async def fake_run_wp(user, root, args, **kw):
        calls.append(list(args))
        return _result()

    monkeypatch.setattr(wordpress, "run_wp", fake_run_wp)
    assert await wordpress.run_action(FakeSite(), "maintenance_off") is None
    assert await wordpress.run_action(FakeSite(), "shuffle_salts") is None
    assert ["maintenance-mode", "deactivate"] in calls
    assert ["config", "shuffle-salts"] in calls


async def test_wp_login_link_uses_eval_reset_key(monkeypatch):
    calls: list[list[str]] = []

    async def fake_run_wp(user, root, args, timeout=120.0, check=True):
        calls.append(list(args))
        if args[:1] == ["eval"]:
            return _result(stdout="https://a.example/wp-login.php?action=rp&key=k&login=admin\n")
        return _result()

    monkeypatch.setattr(wordpress, "run_wp", fake_run_wp)
    url = await wordpress.run_action(FakeSite(), "login_link")
    assert url == "https://a.example/wp-login.php?action=rp&key=k&login=admin"
    assert calls and calls[0][0] == "eval"


async def test_wp_login_link_empty_url_raises(monkeypatch):
    async def fake_run_wp(user, root, args, timeout=120.0, check=True):
        return _result(stdout="")

    monkeypatch.setattr(wordpress, "run_wp", fake_run_wp)
    with pytest.raises(wordpress.WordPressError):
        await wordpress.run_action(FakeSite(), "login_link")


async def test_wp_status_tolerates_garbage_outputs(monkeypatch):
    responses = {
        ("core", "is-installed"): _result(ok=True),
        ("core", "version"): _result(stdout="6.5.1"),
        ("core", "check-update"): _result(stdout="not-json"),
        ("plugin", "list"): _result(stdout="not-a-number"),
        ("theme", "list"): _result(ok=False),
    }

    async def fake_run_wp(user, root, args, **kw):
        return responses[tuple(args[:2])]

    monkeypatch.setattr(wordpress, "run_wp", fake_run_wp)
    status = await wordpress.status(FakeSite())
    assert status.installed and status.version == "6.5.1"
    assert status.update_available is None
    assert status.plugin_count is None and status.theme_count is None


async def test_probe_success_path(monkeypatch):
    class FakeSslObject:
        def getpeercert(self):
            return {"issuer": ((("organizationName", "Test CA"),),), "notAfter": None}

    class FakeWriter:
        def get_extra_info(self, key):
            return FakeSslObject() if key == "ssl_object" else None

        def close(self):
            pass

    async def yes_dns(domain):
        return True

    async def fake_open_connection(*a, **k):
        return None, FakeWriter()

    monkeypatch.setattr(ssl, "_resolves", yes_dns)
    monkeypatch.setattr(ssl.asyncio, "open_connection", fake_open_connection)
    status = await ssl.probe("ok.example")
    assert status.status == "active" and status.issuer == "Test CA"


# --- panel self-backup (Week 23) ------------------------------------------------


async def test_panel_self_backup_snapshot_and_prune(tmp_path, settings, monkeypatch):
    import os
    import sqlite3

    from app.main import create_app
    from app.services import scheduler

    s = settings.model_copy(
        update={
            "backups_root": str(tmp_path / "backups"),
            "database_url": f"sqlite+aiosqlite:///{tmp_path}/panel.db",
        }
    )
    application = create_app(s)
    async with application.router.lifespan_context(application):
        target = await scheduler.self_backup(application)
        assert target is not None and os.path.exists(target)
        # Snapshot is a valid SQLite DB containing the schema.
        con = sqlite3.connect(target)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        assert "users" in tables and "sites" in tables

        # Same day: no duplicate.
        assert await scheduler.self_backup(application) is None

        # Prune: fabricate old snapshots beyond the retention window.
        panel_dir = os.path.join(s.backups_root, "_panel")
        for i in range(1, 10):
            open(os.path.join(panel_dir, f"hosty-201001{i:02d}.db"), "w").close()
        os.unlink(target)
        await scheduler.self_backup(application)
        remaining = sorted(os.listdir(panel_dir))
        assert len(remaining) == scheduler.SELF_BACKUP_KEEP
