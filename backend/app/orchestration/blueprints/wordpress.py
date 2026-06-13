"""WordPress stack blueprint (v2/M5, ADR-013).

The blueprint renders WordPress as a normal Hosty Stack: apache WordPress
container, private MariaDB container, and private Adminer/Filebrowser sidecars.
Day-2 actions use the Podman seam only; the reconciler still owns host state.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.secrets import decrypt_secret, encrypt_secret
from app.db.models import Stack, StackService, Tenant
from app.domain.specs import EndpointSpec, ServiceSpec, StackSpec, VolumeSpec
from app.orchestration.blueprints.base import (
    ActionHandler,
    ActionResult,
    Allocation,
    BackupHooks,
    StackHealth,
)
from app.system import podman, quadlet

WEB = "web"
DB = "db"
ADMINER = "adminer"
FILES = "files"

DB_NAME = "wordpress"
DB_USER = "wordpress"
DB_PASSWORD_SECRET = "db_password"

WP_CLI_IMAGE = (
    "docker.io/library/wordpress:cli-php8.3"
    "@sha256:744d4cbfb63d6ed90808cf87d99f822ed569c9cec714f602ca7d2fc955892455"
)
ADMINER_IMAGE = (
    "docker.io/library/adminer:4"
    "@sha256:ee94dcfafed929cdd6e0bc25e74ce1bf92b71e5c9a7e94e9fee5e3993e7d49dd"
)
FILES_IMAGE = (
    "docker.io/filebrowser/filebrowser:v2"
    "@sha256:9805b21cf910f3ef6f4a1c8f441f1dd6cc4197136f9541fe2a1ab6d050706e4b"
)
MARIADB_IMAGE = (
    "docker.io/library/mariadb:11.4"
    "@sha256:1b46b73d4b629022dfa29e6db3bb0d63b5df714fc3bfbe5057d63d76d8f6054b"
)

SALT_ENV_KEYS = (
    "WORDPRESS_AUTH_KEY",
    "WORDPRESS_SECURE_AUTH_KEY",
    "WORDPRESS_LOGGED_IN_KEY",
    "WORDPRESS_NONCE_KEY",
    "WORDPRESS_AUTH_SALT",
    "WORDPRESS_SECURE_AUTH_SALT",
    "WORDPRESS_LOGGED_IN_SALT",
    "WORDPRESS_NONCE_SALT",
)

PHP_SERIES = ("8.2", "8.3", "8.4")
DEFAULT_PHP = "8.3"


def _wp_image(php_version: str) -> str:
    digest = {
        "8.2": "1e6215749283955d5c9ffea6c297651ed23cdfdbb91677ad7abd705b2682f2cf",
        "8.3": "30bff39330d1693b0ce13d32fc9b7bb67193064f040b7d60d3494e136fa599d4",
        "8.4": "da2a1ff20daa435abf260853ebfd829b1f5f9b8400938940c7393f786a63bf94",
    }[php_version]
    return f"docker.io/library/wordpress:6.8-php{php_version}-apache@sha256:{digest}"


def php_series_of(image: str) -> str:
    match = re.search(r"php(8\.[234])-apache", image or "")
    return match.group(1) if match else DEFAULT_PHP


def derive_salts(seed: str) -> dict[str, str]:
    return {
        key: hashlib.sha256(f"hosty-wp-salt:{seed}:{key}".encode()).hexdigest()
        for key in SALT_ENV_KEYS
    }


class WordPressInputs(BaseModel):
    domain: str = Field(max_length=253)
    title: str = Field(min_length=1, max_length=200)
    admin_user: str = Field(min_length=3, max_length=60)
    admin_email: str = Field(max_length=254)
    php_version: str = Field(default=DEFAULT_PHP)
    locale: str = Field(default="en_US")
    behind_cloudflare: bool = False

    @field_validator("admin_email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email address")
        return value

    @field_validator("admin_user")
    @classmethod
    def valid_admin_user(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9_.\-@ ]+", value):
            raise ValueError("Letters, digits, _ . - @ and spaces only")
        return value

    @field_validator("php_version")
    @classmethod
    def valid_php_version(cls, value: str) -> str:
        if value not in PHP_SERIES:
            raise ValueError("Unsupported PHP version")
        return value

    @field_validator("locale")
    @classmethod
    def valid_locale(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z]{2}_[A-Z]{2}", value):
            raise ValueError("Invalid locale")
        return value


def _secret(inputs: dict[str, Any], key: str, fallback: str = "") -> str:
    value = inputs.get(key)
    return str(value) if value else fallback


def _domain(inputs: dict[str, Any]) -> str:
    return _secret(inputs, "domain")


def _tenant_for(owner_id: int) -> str:
    return f"hosty-t-{int(owner_id)}"


def _encrypt_env(env: dict[str, str], settings: Settings) -> str:
    return encrypt_secret(json.dumps(env), settings.secret_key)


def _decrypt_env(service: StackService, settings: Settings) -> dict[str, str]:
    if not service.env_encrypted:
        return {}
    return json.loads(decrypt_secret(service.env_encrypted, settings.secret_key))


async def _uid_for(db: AsyncSession, stack: Stack) -> int:
    if stack.owner_id is None:
        raise RuntimeError("Stack has no owner")
    tenant = (
        await db.execute(select(Tenant).where(Tenant.linux_user == _tenant_for(stack.owner_id)))
    ).scalar_one_or_none()
    if tenant is None or tenant.uid is None:
        raise RuntimeError("Stack tenant is not provisioned")
    return tenant.uid


async def _service(db: AsyncSession, stack: Stack, name: str) -> StackService:
    row = (
        await db.execute(
            select(StackService).where(StackService.stack_id == stack.id, StackService.name == name)
        )
    ).scalar_one()
    return row


async def _wp_cli(
    *,
    db: AsyncSession,
    stack: Stack,
    args: list[str],
    timeout: float = 300,
) -> podman.runner.CommandResult:
    uid = await _uid_for(db, stack)
    if stack.owner_id is None:
        raise RuntimeError("Stack has no owner")
    tenant = _tenant_for(stack.owner_id)
    html_dir = quadlet.volume_host_dir(tenant, stack.name, "html")
    env_file = quadlet.env_file_path(tenant, stack.name, WEB)
    return await podman.run_transient(
        uid,
        WP_CLI_IMAGE,
        ["wp", *args],
        network=f"hosty-{stack.name}",
        volumes=[(html_dir, "/var/www/html")],
        env_file=env_file,
        user="33:33",
        workdir="/var/www/html",
        timeout=timeout,
    )


def _result_from_command(command, *, ok_message: str = "") -> ActionResult:
    if command.returncode != 0:
        return ActionResult(ok=False, message=command.stderr.strip() or "command failed")
    return ActionResult(ok=True, message=ok_message, data={})


async def _is_installed(db: AsyncSession, stack: Stack) -> bool:
    result = await _wp_cli(db=db, stack=stack, args=["core", "is-installed"], timeout=60)
    return result.returncode == 0


async def install(*, db, settings, stack: Stack, inputs: dict, params: dict) -> ActionResult:
    if await _is_installed(db, stack):
        return ActionResult(ok=False, message="WordPress is already installed")
    password = secrets.token_urlsafe(18)
    result = await _wp_cli(
        db=db,
        stack=stack,
        args=[
            "core",
            "install",
            f"--url=https://{_domain(inputs)}",
            f"--title={_secret(inputs, 'title')}",
            f"--admin_user={_secret(inputs, 'admin_user')}",
            f"--admin_password={password}",
            f"--admin_email={_secret(inputs, 'admin_email')}",
            f"--locale={_secret(inputs, 'locale', 'en_US')}",
            "--skip-email",
        ],
        timeout=600,
    )
    if result.returncode != 0:
        return ActionResult(ok=False, message=result.stderr.strip() or "install failed")
    return ActionResult(ok=True, show_once={"admin_password": password})


async def status(*, db, settings, stack: Stack, inputs: dict, params: dict) -> ActionResult:
    if not await _is_installed(db, stack):
        return ActionResult(ok=True, data={"installed": False})

    async def run(args: list[str]) -> str:
        result = await _wp_cli(db=db, stack=stack, args=args, timeout=120)
        return result.stdout.strip() if result.returncode == 0 else ""

    update_raw = await run(["core", "check-update", "--format=json"])
    try:
        updates = json.loads(update_raw or "[]")
    except json.JSONDecodeError:
        updates = []
    return ActionResult(
        ok=True,
        data={
            "installed": True,
            "version": await run(["core", "version"]),
            "update_available": updates[0].get("version") if updates else None,
            "plugin_updates": int(
                await run(["plugin", "list", "--update=available", "--format=count"]) or 0
            ),
            "theme_updates": int(
                await run(["theme", "list", "--update=available", "--format=count"]) or 0
            ),
        },
    )


async def _simple_action(db: AsyncSession, stack: Stack, args: list[str]) -> ActionResult:
    return _result_from_command(await _wp_cli(db=db, stack=stack, args=args))


async def maintenance_on(**kwargs) -> ActionResult:
    return await _simple_action(kwargs["db"], kwargs["stack"], ["maintenance-mode", "activate"])


async def maintenance_off(**kwargs) -> ActionResult:
    return await _simple_action(kwargs["db"], kwargs["stack"], ["maintenance-mode", "deactivate"])


async def core_update(**kwargs) -> ActionResult:
    result = await _simple_action(kwargs["db"], kwargs["stack"], ["core", "update"])
    if not result.ok:
        return result
    return await _simple_action(kwargs["db"], kwargs["stack"], ["core", "update-db"])


async def plugins_update(**kwargs) -> ActionResult:
    return await _simple_action(kwargs["db"], kwargs["stack"], ["plugin", "update", "--all"])


async def themes_update(**kwargs) -> ActionResult:
    return await _simple_action(kwargs["db"], kwargs["stack"], ["theme", "update", "--all"])


async def admin_login_link(
    *, db, settings, stack: Stack, inputs: dict, params: dict
) -> ActionResult:
    result = await _wp_cli(db=db, stack=stack, args=["eval", "login-link"], timeout=120)
    if result.returncode != 0 or not result.stdout.strip():
        return ActionResult(ok=False, message=result.stderr.strip() or "login link unavailable")
    return ActionResult(ok=True, show_once={"login_url": result.stdout.strip()})


async def rotate_salts(
    *, db, settings: Settings, stack: Stack, inputs: dict, params: dict
) -> ActionResult:
    service = await _service(db, stack, WEB)
    env = _decrypt_env(service, settings)
    env.update(derive_salts(secrets.token_urlsafe(24)))
    service.env_encrypted = _encrypt_env(env, settings)
    stack.generation += 1
    await db.commit()
    return ActionResult(ok=True, message="Salts rotated")


async def switch_php(*, db, settings, stack: Stack, inputs: dict, params: dict) -> ActionResult:
    php_version = str(params.get("php_version", ""))
    if php_version not in PHP_SERIES:
        return ActionResult(ok=False, message="Unsupported PHP version")
    service = await _service(db, stack, WEB)
    if php_series_of(service.image) == php_version:
        return ActionResult(ok=True, message="PHP version already selected")
    service.image = _wp_image(php_version)
    stack.generation += 1
    await db.commit()
    return ActionResult(ok=True, message=f"Switched to PHP {php_version}")


async def _db_password(db: AsyncSession, stack: Stack, settings: Settings) -> str:
    service = await _service(db, stack, DB)
    env = _decrypt_env(service, settings)
    return env["MARIADB_PASSWORD"]


async def pre_backup(*, db, settings: Settings, stack: Stack, directory: Path) -> None:
    uid = await _uid_for(db, stack)
    password = await _db_password(db, stack, settings)
    result = await podman.exec_in(
        uid,
        f"{stack.name}-{DB}",
        ["mariadb-dump", "-u", DB_USER, DB_NAME],
        env={"MYSQL_PWD": password},
        timeout=600,
        stdout_path=str(directory / "wordpress.sql"),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "database dump failed")


async def post_restore(*, db, settings: Settings, stack: Stack, directory: Path) -> None:
    dump = directory / "wordpress.sql"
    if not dump.exists():
        return
    uid = await _uid_for(db, stack)
    password = await _db_password(db, stack, settings)
    result = await podman.exec_in(
        uid,
        f"{stack.name}-{DB}",
        ["mariadb", "-u", DB_USER, DB_NAME],
        env={"MYSQL_PWD": password},
        timeout=600,
        stdin_path=str(dump),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "database import failed")


class WordPressBlueprint:
    id = "wordpress"
    version = 1
    category = "CMS"
    icon = "wordpress"
    display_name = "WordPress"
    description = "Deploy a fully containerized WordPress stack."

    def inputs(self) -> type[WordPressInputs]:
        return WordPressInputs

    def secrets_needed(self, inputs: WordPressInputs) -> list[str]:
        return [DB_PASSWORD_SECRET]

    def render(self, name: str, inputs: WordPressInputs, alloc: Allocation) -> StackSpec:
        password = alloc.secrets[DB_PASSWORD_SECRET]
        salts = derive_salts(password)
        web_env = {
            "WORDPRESS_DB_HOST": f"{name}-{DB}",
            "WORDPRESS_DB_NAME": DB_NAME,
            "WORDPRESS_DB_USER": DB_USER,
            "WORDPRESS_DB_PASSWORD": password,
            **salts,
        }
        db_env = {
            "MARIADB_RANDOM_ROOT_PASSWORD": "1",
            "MARIADB_DATABASE": DB_NAME,
            "MARIADB_USER": DB_USER,
            "MARIADB_PASSWORD": password,
        }
        services = (
            ServiceSpec(
                name=WEB,
                image=_wp_image(inputs.php_version),
                env=tuple(sorted(web_env.items())),
                internal_port=80,
                is_web=True,
            ),
            ServiceSpec(
                name=DB,
                image=MARIADB_IMAGE,
                env=tuple(sorted(db_env.items())),
            ),
            ServiceSpec(
                name=ADMINER,
                image=ADMINER_IMAGE,
                internal_port=8080,
            ),
            ServiceSpec(
                name=FILES,
                image=FILES_IMAGE,
                internal_port=80,
            ),
        )
        return StackSpec(
            name=name,
            tenant=alloc.tenant,
            loopback_ip=alloc.loopback_ip,
            services=services,
            volumes=(
                VolumeSpec(name="html", service=WEB, mount_path="/var/www/html"),
                VolumeSpec(name="db-data", service=DB, mount_path="/var/lib/mysql"),
                VolumeSpec(name="html", service=FILES, mount_path="/srv"),
            ),
            endpoints=(
                EndpointSpec(
                    domain=inputs.domain,
                    service=WEB,
                    behind_cloudflare=inputs.behind_cloudflare,
                ),
            ),
        )

    def actions(self) -> dict[str, ActionHandler]:
        return {
            "install": install,
            "status": status,
            "maintenance_on": maintenance_on,
            "maintenance_off": maintenance_off,
            "core_update": core_update,
            "plugins_update": plugins_update,
            "themes_update": themes_update,
            "admin_login_link": admin_login_link,
            "rotate_salts": rotate_salts,
            "switch_php": switch_php,
        }

    def backup_hooks(self) -> BackupHooks | None:
        return BackupHooks(pre_backup=pre_backup, post_restore=post_restore, databases=(DB_NAME,))

    def health(self, observed_active: dict[str, bool]) -> StackHealth:
        for service in (WEB, DB):
            if not observed_active.get(service):
                return StackHealth(healthy=False, detail=f"{service} service is not running")
        return StackHealth(healthy=True)
