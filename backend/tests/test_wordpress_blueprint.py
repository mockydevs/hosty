"""M5 gate (v2, ADR-013): WordPress blueprint at v1 parity, against the
FakeHost (units/systemd seams) + a fake Podman CLI seam (`run_transient` /
`exec_in`). Mirrors every v1 WordPress API test from test_php_wp_api.py:

  v1 test                                        → here
  test_wp_status_not_installed                   → test_status_not_installed
  test_wp_install_happy_path                     → test_install_happy_path
  test_wp_install_failure_rolls_back_db_and_files→ test_install_failure_is_undo_free_and_retryable
  test_wp_install_conflicts_when_already_installed → test_install_twice_reports_already_installed
  test_wp_install_validates_fields               → test_create_validates_inputs (422 at create)
  test_wp_action_requires_install                → test_actions_fail_cleanly_before_install
  test_wp_actions_run                            → test_actions_run / unknown → 404
  test_wp_status_unhealthy_when_..._check_fails  → test_health_projection

v2 has no install rollback by design (undo-free convergence): a failed
install reports cleanly and a retry succeeds. PHP switching and salt
rotation are desired-state edits the reconciler applies — both covered.
"""

from __future__ import annotations

import pytest

from app.orchestration.blueprints import get_blueprint
from app.orchestration.blueprints.wordpress import (
    SALT_ENV_KEYS,
    derive_salts,
    php_series_of,
)
from app.services import backup
from app.services import stacks as stacks_service
from app.system import podman
from app.system.runner import CommandResult
from tests.test_orchestration import FakeHost
from tests.test_stacks_api import FakeCaddy

LOGIN_URL = "https://wp.example.com/wp-login.php?action=rp&key=K&login=admin"


class FakeWpCli:
    """Fake `podman run --rm wordpress:cli` + `podman exec` seam."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []  # wp args per invocation
        self.runs: list[dict] = []  # full transport kwargs per invocation
        self.execs: list[dict] = []
        self.installed = False
        self.fail_on: str | None = None  # first wp arg to fail on
        self.exec_fail = False

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = self

        def result(args: list[str], ok: bool, stdout: str = "") -> CommandResult:
            return CommandResult(
                argv=("wp", *args),
                returncode=0 if ok else 1,
                stdout=stdout,
                stderr="" if ok else "boom",
                duration_ms=1.0,
            )

        async def run_transient(uid, image, command, *, timeout=300, **kwargs):
            assert command[0] == "wp"
            args = command[1:]
            fake.calls.append(list(args))
            fake.runs.append({"uid": uid, "image": image, **kwargs})
            ok, stdout = True, ""
            if args[:2] == ["core", "is-installed"]:
                ok = fake.installed
            elif fake.fail_on and args[0] == fake.fail_on:
                ok = False
            elif args[:2] == ["core", "install"]:
                fake.installed = True
            elif args[0] == "eval":
                ok, stdout = fake.installed, LOGIN_URL if fake.installed else ""
            elif args[:2] == ["core", "version"]:
                stdout = "6.8"
            elif args[:2] == ["core", "check-update"]:
                stdout = '[{"version": "6.9"}]'
            elif args[:2] == ["plugin", "list"]:
                stdout = "2"
            elif args[:2] == ["theme", "list"]:
                stdout = "1"
            return result(args, ok, stdout)

        async def exec_in(uid, container, command, *, env=None, timeout=120, **io):
            fake.execs.append(
                {"uid": uid, "container": container, "command": list(command), "env": env, **io}
            )
            if fake.exec_fail:
                return CommandResult(
                    argv=tuple(command), returncode=1, stdout="", stderr="boom", duration_ms=1.0
                )
            if io.get("stdout_path"):
                from pathlib import Path

                Path(io["stdout_path"]).write_text("-- fake dump\n", encoding="utf-8")
            return CommandResult(
                argv=tuple(command), returncode=0, stdout="", stderr="", duration_ms=1.0
            )

        monkeypatch.setattr(podman, "run_transient", run_transient)
        monkeypatch.setattr(podman, "exec_in", exec_in)


@pytest.fixture
def stack_host(app, settings, monkeypatch) -> FakeHost:
    host = FakeHost()
    host.install(monkeypatch)
    host.caddy = FakeCaddy()

    async def observe(db):
        return host.build_observed()

    settings.reconcile_concurrency = 1  # sqlite: one writer
    app.state.reconciler = stacks_service.build_reconciler(
        app.state.sessionmaker, settings, observe=observe, caddy_client=host.caddy
    )
    return host


@pytest.fixture
def wp_cli(monkeypatch) -> FakeWpCli:
    fake = FakeWpCli()
    fake.install(monkeypatch)
    return fake


WP_BODY = {
    "name": "blog",
    "blueprint_id": "wordpress",
    "inputs": {
        "domain": "wp.example.com",
        "title": "My Blog",
        "admin_user": "admin",
        "admin_email": "don@example.com",
    },
}


async def create_wp_stack(client) -> int:
    resp = await client.post("/api/stacks", json=WP_BODY)
    assert resp.status_code == 202, resp.text
    op = (await client.get(f"/api/operations/{resp.json()['operation_id']}")).json()
    assert op["status"] == "succeeded", op
    return resp.json()["stack"]["id"]


async def act(client, stack_id: int, action: str, params: dict | None = None) -> dict:
    body = {"params": params} if params is not None else None
    resp = await client.post(f"/api/stacks/{stack_id}/actions/{action}", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- composition ---------------------------------------------------------------------


async def test_stack_composition(admin_client, stack_host, wp_cli):
    stack_id = await create_wp_stack(admin_client)
    stack = (await admin_client.get(f"/api/stacks/{stack_id}")).json()
    services = {s["name"]: s for s in stack["services"]}

    assert "php8.3-apache" in services["web"]["image"]
    assert services["web"]["internal_port"] == 80 and services["web"]["host_port"]
    # The DB is stack-internal: no published port, unreachable from the host.
    assert services["db"]["internal_port"] is None and services["db"]["host_port"] is None
    assert {v["name"]: v["mount_path"] for v in stack["volumes"]} == {
        "html": "/var/www/html",
        "db-data": "/var/lib/mysql",
    }
    assert stack["endpoints"][0]["domain"] == "wp.example.com"

    # Web env carries DB credentials + all 8 panel-managed salts (env file,
    # never unit text); db env creates the database with the same password.
    env_texts = list(stack_host.env_files.values())
    web_env = next(t for t in env_texts if "WORDPRESS_DB_HOST=blog-db" in t)
    db_env = next(t for t in env_texts if "MARIADB_DATABASE=wordpress" in t)
    for key in SALT_ENV_KEYS:
        assert f"{key}=" in web_env
    password = next(
        line.split("=", 1)[1] for line in web_env.splitlines() if "WORDPRESS_DB_PASSWORD" in line
    )
    assert f"MARIADB_PASSWORD={password}" in db_env
    assert "MARIADB_RANDOM_ROOT_PASSWORD=1" in db_env


def test_derive_salts_is_deterministic_and_distinct():
    salts = derive_salts("seed")
    assert set(salts) == set(SALT_ENV_KEYS)
    assert len(set(salts.values())) == len(SALT_ENV_KEYS)  # all distinct
    assert derive_salts("seed") == salts  # render purity
    assert derive_salts("other") != salts


# --- v1 parity ------------------------------------------------------------------------


async def test_status_not_installed(admin_client, stack_host, wp_cli):
    stack_id = await create_wp_stack(admin_client)
    result = await act(admin_client, stack_id, "status")
    assert result["ok"] is True
    assert result["data"] == {"installed": False}


async def test_install_happy_path(admin_client, stack_host, wp_cli):
    stack_id = await create_wp_stack(admin_client)
    result = await act(admin_client, stack_id, "install")
    assert result["ok"] is True, result

    install = next(c for c in wp_cli.calls if c[:2] == ["core", "install"])
    assert "--url=https://wp.example.com" in install
    assert "--title=My Blog" in install
    assert "--skip-email" in install
    # The admin password is generated, returned show-once, and matches argv.
    password = result["show_once"]["admin_password"]
    assert f"--admin_password={password}" in install

    # WP-CLI ran in the stack's network, sharing the html volume and web env.
    run = wp_cli.runs[-1]
    assert run["network"] == "hosty-blog"
    assert run["volumes"] == [(run["volumes"][0][0], "/var/www/html")]
    assert "/stacks/blog/volumes/html" in run["volumes"][0][0]
    assert run["env_file"].endswith("/stacks/blog/env/web.env")
    assert run["user"] == "33:33"

    status = await act(admin_client, stack_id, "status")
    assert status["data"]["installed"] is True
    assert status["data"]["version"] == "6.8"
    assert status["data"]["update_available"] == "6.9"
    assert status["data"]["plugin_updates"] == 2
    assert status["data"]["theme_updates"] == 1


async def test_install_failure_is_undo_free_and_retryable(admin_client, stack_host, wp_cli):
    """v1 rolled back DB + files; v2 is undo-free by design — the action
    reports failure (no 500), nothing is torn down, and a retry succeeds."""
    stack_id = await create_wp_stack(admin_client)
    wp_cli.fail_on = "core"
    result = await act(admin_client, stack_id, "install")
    assert result["ok"] is False
    assert "boom" in result["message"]

    stack = (await admin_client.get(f"/api/stacks/{stack_id}")).json()
    assert stack["status"] == "ready"  # the stack itself is unharmed

    wp_cli.fail_on = None
    result = await act(admin_client, stack_id, "install")
    assert result["ok"] is True


async def test_install_twice_reports_already_installed(admin_client, stack_host, wp_cli):
    stack_id = await create_wp_stack(admin_client)
    assert (await act(admin_client, stack_id, "install"))["ok"] is True
    result = await act(admin_client, stack_id, "install")
    assert result["ok"] is False
    assert "already installed" in result["message"].lower()


async def test_create_validates_inputs(admin_client, stack_host, wp_cli):
    for patch in [
        {"admin_email": "not-an-email"},
        {"locale": "en-US"},
        {"php_version": "7.4"},
        {"title": ""},
        {"admin_user": "x"},
    ]:
        body = {**WP_BODY, "inputs": {**WP_BODY["inputs"], **patch}}
        resp = await admin_client.post("/api/stacks", json=body)
        assert resp.status_code == 422, patch


async def test_actions_fail_cleanly_before_install(admin_client, stack_host, wp_cli):
    stack_id = await create_wp_stack(admin_client)
    result = await act(admin_client, stack_id, "admin_login_link")
    assert result["ok"] is False  # wp eval fails on an uninstalled site — no 500


async def test_actions_run(admin_client, stack_host, wp_cli):
    stack_id = await create_wp_stack(admin_client)
    await act(admin_client, stack_id, "install")
    wp_cli.calls.clear()

    assert (await act(admin_client, stack_id, "maintenance_on"))["ok"] is True
    assert ["maintenance-mode", "activate"] in wp_cli.calls
    assert (await act(admin_client, stack_id, "maintenance_off"))["ok"] is True
    assert ["maintenance-mode", "deactivate"] in wp_cli.calls

    assert (await act(admin_client, stack_id, "core_update"))["ok"] is True
    assert ["core", "update"] in wp_cli.calls and ["core", "update-db"] in wp_cli.calls
    assert (await act(admin_client, stack_id, "plugins_update"))["ok"] is True
    assert ["plugin", "update", "--all"] in wp_cli.calls
    assert (await act(admin_client, stack_id, "themes_update"))["ok"] is True
    assert ["theme", "update", "--all"] in wp_cli.calls

    result = await act(admin_client, stack_id, "admin_login_link")
    assert result["ok"] is True
    assert result["show_once"]["login_url"] == LOGIN_URL

    resp = await admin_client.post(f"/api/stacks/{stack_id}/actions/nonsense")
    assert resp.status_code == 404


def test_health_projection():
    bp = get_blueprint("wordpress")
    assert bp.health({"web": True, "db": True}).healthy is True
    sick = bp.health({"web": True, "db": False})
    assert sick.healthy is False and "db" in sick.detail
    assert bp.health({}).healthy is False


# --- v2-specific: desired-state actions ------------------------------------------------


async def test_rotate_salts_edits_env_and_restarts(admin_client, stack_host, wp_cli):
    stack_id = await create_wp_stack(admin_client)
    web_env_path = next(p for p in stack_host.env_files if p.endswith("web.env"))
    before = stack_host.env_files[web_env_path]

    result = await act(admin_client, stack_id, "rotate_salts")
    assert result["ok"] is True

    stack = (await admin_client.get(f"/api/stacks/{stack_id}")).json()
    assert stack["status"] == "ready"  # background converge already applied it
    assert stack["generation"] == stack["observed_generation"] == 2
    after = stack_host.env_files[web_env_path]
    assert after != before  # salts changed on the host
    assert ("hosty-t-1", "blog-web.service") in stack_host.active


async def test_switch_php_repins_image(admin_client, stack_host, wp_cli):
    stack_id = await create_wp_stack(admin_client)

    result = await act(admin_client, stack_id, "switch_php", params={"php_version": "8.4"})
    assert result["ok"] is True

    stack = (await admin_client.get(f"/api/stacks/{stack_id}")).json()
    web = next(s for s in stack["services"] if s["name"] == "web")
    assert "php8.4-apache" in web["image"]
    assert stack["status"] == "ready" and stack["generation"] == 2

    assert (await act(admin_client, stack_id, "switch_php", params={"php_version": "8.4"}))[
        "ok"
    ] is True  # no-op
    bad = await act(admin_client, stack_id, "switch_php", params={"php_version": "7.4"})
    assert bad["ok"] is False


def test_php_series_of():
    assert php_series_of("docker.io/library/wordpress:6.8-php8.2-apache") == "8.2"
    assert php_series_of("garbage") == "8.3"  # falls back to the default


# --- backup hooks ----------------------------------------------------------------------


async def test_backup_hooks_dump_and_import(
    admin_client, stack_host, wp_cli, app, settings, tmp_path
):
    stack_id = await create_wp_stack(admin_client)
    bp = get_blueprint("wordpress")
    hooks = bp.backup_hooks()
    assert hooks is not None and hooks.databases == ("wordpress",)

    async with app.state.sessionmaker() as db:
        from app.db.models import Stack

        stack = await db.get(Stack, stack_id)
        await hooks.pre_backup(db=db, settings=settings, stack=stack, directory=tmp_path)
        dump = wp_cli.execs[-1]
        assert dump["container"] == "blog-db"
        assert dump["command"][0] == "mariadb-dump"
        # Credentials travel via env, never argv.
        assert "MYSQL_PWD" in dump["env"]
        assert not any(
            "MYSQL_PWD" in part or dump["env"]["MYSQL_PWD"] in part for part in dump["command"]
        )
        assert (tmp_path / "wordpress.sql").read_text(encoding="utf-8") == "-- fake dump\n"

        await hooks.post_restore(db=db, settings=settings, stack=stack, directory=tmp_path)
        restore = wp_cli.execs[-1]
        assert restore["command"][0] == "mariadb"
        assert restore["stdin_path"] == str(tmp_path / "wordpress.sql")

        # A volumes-only backup (no dump file) restores without an import.
        count = len(wp_cli.execs)
        await hooks.post_restore(
            db=db, settings=settings, stack=stack, directory=tmp_path / "empty"
        )
        assert len(wp_cli.execs) == count


async def test_stack_backup_restore_api(
    admin_client, stack_host, wp_cli, settings, tmp_path, monkeypatch
):
    settings.backups_root = str(tmp_path / "backups")
    settings.restore_staging_root = str(tmp_path / "restore")
    restored: list[tuple[str, str]] = []

    async def archive_files(source, destination):
        assert source == "/home/hosty-t-1/stacks/blog/volumes"
        (destination / backup.FILES_ARCHIVE).write_bytes(b"volume archive")

    async def restore_tree(directory, destination, *, allowed_root, staging_root, owner):
        assert (directory / backup.FILES_ARCHIVE).read_bytes() == b"volume archive"
        restored.append((destination, allowed_root))
        assert staging_root == settings.restore_staging_root
        assert owner == "hosty-t-1"

    monkeypatch.setattr(backup, "archive_files", archive_files)
    monkeypatch.setattr(backup, "restore_tree", restore_tree)
    monkeypatch.setattr(
        backup,
        "validate_managed_directory",
        lambda path, *, allowed_root: path,
    )

    stack_id = await create_wp_stack(admin_client)
    response = await admin_client.post(f"/api/stacks/{stack_id}/backups")
    assert response.status_code == 202, response.text
    operation_url = f"/api/operations/{response.json()['operation_id']}"
    operation = (await admin_client.get(operation_url)).json()
    assert operation["status"] == "succeeded", operation
    assert ("hosty-t-1", "blog-web.service") in stack_host.active
    assert ("hosty-t-1", "blog-db.service") in stack_host.active

    listed = (await admin_client.get(f"/api/stacks/{stack_id}/backups")).json()
    assert len(listed) == 1
    backup_id = listed[0]["backup_id"]
    assert listed[0]["wordpress"] is True

    wrong = await admin_client.post(
        f"/api/stacks/{stack_id}/backups/{backup_id}/restore",
        json={"scope": "full", "confirm_domain": "wrong"},
    )
    assert wrong.status_code == 409

    wp_cli.execs.clear()
    response = await admin_client.post(
        f"/api/stacks/{stack_id}/backups/{backup_id}/restore",
        json={"scope": "full", "confirm_domain": "blog"},
    )
    assert response.status_code == 202, response.text
    operation_url = f"/api/operations/{response.json()['operation_id']}"
    operation = (await admin_client.get(operation_url)).json()
    assert operation["status"] == "succeeded", operation
    assert restored == [("/home/hosty-t-1/stacks/blog/volumes", "/home/hosty-t-1/stacks/blog")]
    assert wp_cli.execs[-1]["command"][0] == "mariadb"
    assert ("hosty-t-1", "blog-web.service") in stack_host.active
    assert ("hosty-t-1", "blog-db.service") in stack_host.active

    response = await admin_client.request(
        "DELETE",
        f"/api/stacks/{stack_id}/backups/{backup_id}",
        json={"confirm_id": backup_id},
    )
    assert response.status_code == 204
    assert (await admin_client.get(f"/api/stacks/{stack_id}/backups")).json() == []
