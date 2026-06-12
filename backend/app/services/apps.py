"""Containerized apps (Phase 12a, ADR-011): provisioning pipeline + lifecycle.

Mirrors services/sites.py: create runs as ordered steps with compensating
undo; delete is idempotent and retryable. All engine calls go through
app.system.docker; this module owns naming, port allocation, env-at-rest
encryption, and the app directory layout:

    /var/lib/hosty/apps/<id>/env             root-only env file (0600-ish 0640)
    /var/lib/hosty/apps/<id>/volumes/<name>  bind-mounted into the container
"""

from __future__ import annotations

import json
import re

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.secrets import decrypt_secret, encrypt_secret
from app.db.models import App, Operation
from app.services.caddy import CaddyClient
from app.services.sites import (
    _caddy_client,
    _finish,
    _run_pipeline,
    _Step,
    build_full_config,
)
from app.system import docker, fs

log = structlog.get_logger("hosty.apps")

APP_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")
VOLUME_NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$")

CREATE_APP_STEPS = [
    ("image", "Pull container image"),
    ("app_dir", "Create app directory and volumes"),
    ("network", "Create isolated network"),
    ("container", "Start container"),
    ("caddy", "Publish vhost to Caddy"),
    ("finalize", "Activate app"),
]

DELETE_APP_STEPS = [
    ("caddy", "Remove vhost from Caddy"),
    ("container", "Stop and remove container"),
    ("network", "Remove network"),
    ("app_dir", "Delete app directory"),
    ("finalize", "Remove app record"),
]


class AppValidationError(ValueError):
    pass


def validate_app_name(raw: str) -> str:
    if not isinstance(raw, str) or not APP_NAME_RE.fullmatch(raw.strip().lower()):
        raise AppValidationError(
            "App name must be 1-32 lowercase letters, digits, or hyphens "
            "(no leading/trailing hyphen)"
        )
    return raw.strip().lower()


def validate_volumes(raw: list[dict]) -> list[dict]:
    seen: set[str] = set()
    cleaned: list[dict] = []
    for item in raw:
        name = str(item.get("name", "")).strip().lower()
        if not VOLUME_NAME_RE.fullmatch(name):
            raise AppValidationError(f"Invalid volume name: {name!r}")
        if name in seen:
            raise AppValidationError(f"Duplicate volume name: {name!r}")
        seen.add(name)
        mount_path = docker.validate_mount_path(str(item.get("mount_path", "")))
        cleaned.append({"name": name, "mount_path": mount_path})
    if len(cleaned) > 10:
        raise AppValidationError("At most 10 volumes per app")
    return cleaned


def validate_env(raw: dict[str, str]) -> dict[str, str]:
    if len(raw) > 100:
        raise AppValidationError("At most 100 environment variables per app")
    cleaned: dict[str, str] = {}
    for key, value in raw.items():
        docker.validate_env_key(key)
        value = str(value)
        if "\x00" in value or "\n" in value or len(value) > 4096:
            raise AppValidationError(f"Invalid value for environment variable {key}")
        cleaned[key] = value
    return cleaned


# --- layout & helpers -------------------------------------------------------------


def app_dir_for(app_id: int, settings: Settings) -> str:
    return f"{settings.apps_root}/{int(app_id)}"


def env_file_for(app_id: int, settings: Settings) -> str:
    return f"{app_dir_for(app_id, settings)}/env"


def volume_dir_for(app_id: int, name: str, settings: Settings) -> str:
    return f"{app_dir_for(app_id, settings)}/volumes/{name}"


def encrypt_env(env: dict[str, str], settings: Settings) -> str | None:
    return encrypt_secret(json.dumps(env), settings.secret_key) if env else None


def decrypt_env(app: App, settings: Settings) -> dict[str, str]:
    if not app.env_encrypted:
        return {}
    return json.loads(decrypt_secret(app.env_encrypted, settings.secret_key))


def render_env_file(env: dict[str, str]) -> str:
    """docker --env-file format. Values are raw (no quoting layer); newlines
    are rejected at validation, which is the only character that matters."""
    return "".join(f"{key}={value}\n" for key, value in sorted(env.items()))


async def allocate_host_port(db: AsyncSession, settings: Settings) -> int:
    """Lowest free port in the panel's loopback range. The DB is the ledger:
    host_port is UNIQUE, so a race loses at commit, not at runtime."""
    used = set((await db.execute(select(App.host_port))).scalars().all())
    for port in range(settings.app_port_min, settings.app_port_max + 1):
        if port not in used:
            return port
    raise AppValidationError("No free app ports left on this server")


def container_spec_for(app: App, settings: Settings) -> docker.ContainerSpec:
    volumes = tuple(
        (volume_dir_for(app.id, v["name"], settings), v["mount_path"])
        for v in json.loads(app.volumes_json)
    )
    return docker.ContainerSpec(
        name=docker.container_name(app.id),
        image=app.image_digest or app.image,
        internal_port=app.internal_port,
        host_port=app.host_port,
        network=docker.network_name(app.id),
        env_file=env_file_for(app.id, settings) if app.env_encrypted else None,
        volumes=volumes,
        memory_mb=app.memory_mb,
        cpu_percent=app.cpu_percent,
        labels=((docker.APP_ID_LABEL, str(app.id)),),
    )


# --- pipelines --------------------------------------------------------------------


async def run_create_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    app_id: int,
    operation_id: int,
    caddy_client: CaddyClient | None = None,
) -> None:
    """Background entrypoint: provision `app_id`, tracking `operation_id`."""
    async with sessionmaker() as db:
        app = await db.get(App, app_id)
        op = await db.get(Operation, operation_id)
        assert app is not None and op is not None
        op.status = "running"
        await db.commit()

        client = _caddy_client(settings, caddy_client)
        app_dir = app_dir_for(app.id, settings)

        async def do_image() -> None:
            await docker.pull_image(app.image)
            # Pin what we actually run: a later registry re-tag must never
            # silently change this app's code.
            app.image_digest = await docker.resolve_digest(app.image)
            await db.commit()

        async def do_app_dir() -> None:
            await fs.create_dir(app_dir, root=settings.apps_root)
            for volume in json.loads(app.volumes_json):
                await fs.create_dir(
                    volume_dir_for(app.id, volume["name"], settings), root=settings.apps_root
                )
            env = decrypt_env(app, settings)
            if env:
                fs.write_file(
                    env_file_for(app.id, settings), render_env_file(env), root=settings.apps_root
                )

        async def undo_app_dir() -> None:
            await fs.remove_tree(app_dir, root=settings.apps_root)

        async def do_network() -> None:
            await docker.create_network(docker.network_name(app.id))

        async def undo_network() -> None:
            await docker.remove_network(docker.network_name(app.id))

        async def do_container() -> None:
            await docker.run_container(container_spec_for(app, settings))

        async def undo_container() -> None:
            await docker.remove_container(docker.container_name(app.id))

        async def do_caddy() -> None:
            await client.apply(await build_full_config(db, settings))

        async def undo_caddy() -> None:
            await client.apply(await build_full_config(db, settings, exclude_domain=app.domain))

        async def do_finalize() -> None:
            app.status = "running"
            await db.commit()

        ok, error = await _run_pipeline(
            db,
            op,
            [
                _Step("image", do_image, None),
                _Step("app_dir", do_app_dir, undo_app_dir),
                _Step("network", do_network, undo_network),
                _Step("container", do_container, undo_container),
                _Step("caddy", do_caddy, undo_caddy),
                _Step("finalize", do_finalize, None),
            ],
        )
        if ok:
            await _finish(db, op, status="succeeded")
        else:
            app.status = "error"
            app.error_message = error
            await _finish(db, op, status="failed", error=error)


async def run_delete_app(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    app_id: int,
    operation_id: int,
    caddy_client: CaddyClient | None = None,
) -> None:
    """Background entrypoint: tear down `app_id`. Steps are idempotent, so a
    failed delete can simply be retried."""
    async with sessionmaker() as db:
        app = await db.get(App, app_id)
        op = await db.get(Operation, operation_id)
        assert app is not None and op is not None
        op.status = "running"
        await db.commit()

        client = _caddy_client(settings, caddy_client)
        app_dir = app_dir_for(app.id, settings)

        async def do_caddy() -> None:
            await client.apply(await build_full_config(db, settings, exclude_domain=app.domain))

        async def do_container() -> None:
            await docker.remove_container(docker.container_name(app.id))

        async def do_network() -> None:
            await docker.remove_network(docker.network_name(app.id))

        async def do_app_dir() -> None:
            await fs.remove_tree(app_dir, root=settings.apps_root)

        async def do_finalize() -> None:
            await db.delete(app)
            await db.commit()

        ok, error = await _run_pipeline(
            db,
            op,
            [
                _Step("caddy", do_caddy, None),
                _Step("container", do_container, None),
                _Step("network", do_network, None),
                _Step("app_dir", do_app_dir, None),
                _Step("finalize", do_finalize, None),
            ],
        )
        if ok:
            await _finish(db, op, status="succeeded")
        else:
            app.status = "error"
            app.error_message = error
            await _finish(db, op, status="failed", error=error)


# --- lifecycle (synchronous API actions) -------------------------------------------


async def start_app(db: AsyncSession, app: App) -> None:
    await docker.start_container(docker.container_name(app.id))
    app.status = "running"
    app.error_message = None
    await db.commit()


async def stop_app(db: AsyncSession, app: App) -> None:
    await docker.stop_container(docker.container_name(app.id))
    app.status = "stopped"
    await db.commit()


async def restart_app(db: AsyncSession, app: App) -> None:
    await docker.restart_container(docker.container_name(app.id))
    app.status = "running"
    app.error_message = None
    await db.commit()


async def app_logs(app: App, *, tail: int = 200) -> str:
    return await docker.container_logs(docker.container_name(app.id), tail=tail)
