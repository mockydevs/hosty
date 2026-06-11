"""Site provisioning: domain validation and the transactional pipeline.

Create runs as an ordered list of steps, each with a compensating undo. If any
step fails, completed steps are undone in reverse order, the operation is
marked failed, and the site record is flagged `error` — no half-provisioned
state survives.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import utcnow
from app.core.config import Settings
from app.db.models import Operation, Site
from app.services import caddy, mariadb, php_fpm
from app.services.caddy import CaddyClient, SiteSpec
from app.system import fs, users

log = structlog.get_logger("hosty.sites")

LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

INDEX_SKELETON = """\
<?php
http_response_code(200);
?>
<!doctype html>
<title>{domain}</title>
<h1>{domain}</h1>
<p>This site was just created with Hosty. Replace
<code>public_html/index.php</code> with your content.</p>
"""


class DomainValidationError(ValueError):
    pass


def validate_domain(raw: str) -> str:
    """Normalize and validate a domain; accepts unicode (punycode-encoded)."""
    if not isinstance(raw, str):
        raise DomainValidationError("Domain must be a string")
    candidate = raw.strip().rstrip(".").lower()
    if not candidate or len(candidate) > 253:
        raise DomainValidationError("Domain must be 1-253 characters")
    try:
        # IDNA 2003 fallback is fine for v1; rejects spaces, slashes, etc.
        ascii_domain = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DomainValidationError(f"Invalid domain: {raw!r}") from exc
    labels = ascii_domain.split(".")
    if len(labels) < 2:
        raise DomainValidationError("Domain must contain at least one dot (e.g. example.com)")
    for label in labels:
        if not LABEL_RE.fullmatch(label):
            raise DomainValidationError(f"Invalid domain label: {label!r}")
    return ascii_domain


def derive_site_user(domain: str) -> str:
    """Deterministic `site-*` user for a domain; hash suffix avoids collisions."""
    slug = re.sub(r"[^a-z0-9]+", "-", domain).strip("-")
    digest = hashlib.sha256(domain.encode()).hexdigest()[:6]
    return f"site-{slug[:18].rstrip('-')}-{digest}"


def site_dir_for(domain: str, settings: Settings) -> str:
    return f"{settings.sites_root}/{domain}"


def doc_root_for(domain: str, settings: Settings) -> str:
    return f"{site_dir_for(domain, settings)}/public_html"


def spec_for(site: Site, settings: Settings) -> SiteSpec:
    return SiteSpec(
        domain=site.domain,
        doc_root=site.doc_root,
        php_socket=php_fpm.socket_path(site.site_user, site.php_version, settings),
    )


async def _served_specs(db: AsyncSession, settings: Settings) -> list[SiteSpec]:
    """Specs for every site that should currently be served by Caddy."""
    rows = (
        (await db.execute(select(Site).where(Site.status.in_(("active", "provisioning")))))
        .scalars()
        .all()
    )
    return [spec_for(s, settings) for s in rows]


# --- operation step tracking -------------------------------------------------

CREATE_STEPS = [
    ("linux_user", "Create isolated Linux user"),
    ("doc_root", "Create web root and skeleton"),
    ("php_pool", "Configure PHP-FPM pool"),
    ("caddy", "Publish vhost to Caddy"),
    ("finalize", "Activate site"),
]

DELETE_STEPS = [
    ("caddy", "Remove vhost from Caddy"),
    ("php_pool", "Remove PHP-FPM pool"),
    ("database", "Drop WordPress database"),
    ("doc_root", "Delete site files"),
    ("linux_user", "Delete Linux user"),
    ("finalize", "Remove site record"),
]


def initial_steps(steps: list[tuple[str, str]]) -> str:
    return json.dumps([{"name": n, "label": label, "status": "pending"} for n, label in steps])


async def _set_step(db: AsyncSession, op: Operation, name: str, status: str) -> None:
    steps = json.loads(op.steps_json)
    for step in steps:
        if step["name"] == name:
            step["status"] = status
    op.steps_json = json.dumps(steps)
    await db.commit()


async def _finish(
    db: AsyncSession, op: Operation, *, status: str, error: str | None = None
) -> None:
    op.status = status
    op.error = error
    op.finished_at = utcnow()
    await db.commit()


# --- pipelines ----------------------------------------------------------------

Action = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class _Step:
    name: str
    do: Action
    undo: Action | None  # None = nothing to compensate


async def _run_pipeline(
    db: AsyncSession, op: Operation, steps: list[_Step]
) -> tuple[bool, str | None]:
    """Run steps in order; on failure, undo completed steps in reverse.

    Returns (succeeded, error_message).
    """
    completed: list[_Step] = []
    for step in steps:
        await _set_step(db, op, step.name, "running")
        try:
            await step.do()
        except Exception as exc:
            error = f"{step.name}: {exc}"
            log.error("pipeline_step_failed", step=step.name, error=str(exc))
            await _set_step(db, op, step.name, "failed")
            for done in reversed(completed):
                if done.undo is None:
                    continue
                try:
                    await done.undo()
                    await _set_step(db, op, done.name, "rolled_back")
                except Exception as undo_exc:
                    log.error("pipeline_undo_failed", step=done.name, error=str(undo_exc))
            return False, error
        await _set_step(db, op, step.name, "done")
        completed.append(step)
    return True, None


def _caddy_client(settings: Settings, client: CaddyClient | None) -> CaddyClient:
    return client or CaddyClient(settings.caddy_admin_url)


async def run_create_site(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    site_id: int,
    operation_id: int,
    caddy_client: CaddyClient | None = None,
) -> None:
    """Background entrypoint: provision `site_id`, tracking `operation_id`."""
    async with sessionmaker() as db:
        site = await db.get(Site, site_id)
        op = await db.get(Operation, operation_id)
        assert site is not None and op is not None
        op.status = "running"
        await db.commit()

        client = _caddy_client(settings, caddy_client)
        site_dir = site_dir_for(site.domain, settings)

        async def do_user() -> None:
            await users.create(site.site_user)

        async def undo_user() -> None:
            await users.delete(site.site_user)

        async def do_docroot() -> None:
            await fs.create_dir(site.doc_root, root=settings.sites_root)
            fs.write_file(
                f"{site.doc_root}/index.php",
                INDEX_SKELETON.format(domain=site.domain),
                root=settings.sites_root,
            )
            await fs.chown_recursive(site.site_user, site_dir, root=settings.sites_root)

        async def undo_docroot() -> None:
            await fs.remove_tree(site_dir, root=settings.sites_root)

        async def do_pool() -> None:
            await php_fpm.install_pool(
                site.site_user,
                site.php_version,
                settings,
                memory_limit=site.php_memory_limit,
                upload_max_filesize=site.php_upload_max_filesize,
            )

        async def undo_pool() -> None:
            await php_fpm.remove_pool(site.site_user, site.php_version, settings)

        async def do_caddy() -> None:
            await client.apply(caddy.build_config(await _served_specs(db, settings)))

        async def undo_caddy() -> None:
            specs = [s for s in await _served_specs(db, settings) if s.domain != site.domain]
            await client.apply(caddy.build_config(specs))

        async def do_finalize() -> None:
            site.status = "active"
            op.site_id = site.id
            await db.commit()

        ok, error = await _run_pipeline(
            db,
            op,
            [
                _Step("linux_user", do_user, undo_user),
                _Step("doc_root", do_docroot, undo_docroot),
                _Step("php_pool", do_pool, undo_pool),
                _Step("caddy", do_caddy, undo_caddy),
                _Step("finalize", do_finalize, None),
            ],
        )
        if ok:
            await _finish(db, op, status="succeeded")
        else:
            site.status = "error"
            site.error_message = error
            await _finish(db, op, status="failed", error=error)


async def run_delete_site(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    site_id: int,
    operation_id: int,
    caddy_client: CaddyClient | None = None,
) -> None:
    """Background entrypoint: tear down `site_id`. Steps are idempotent, so a
    failed delete can simply be retried — no compensation that would recreate
    half-deleted resources."""
    async with sessionmaker() as db:
        site = await db.get(Site, site_id)
        op = await db.get(Operation, operation_id)
        assert site is not None and op is not None
        op.status = "running"
        await db.commit()

        client = _caddy_client(settings, caddy_client)
        site_dir = site_dir_for(site.domain, settings)

        async def do_caddy() -> None:
            specs = [s for s in await _served_specs(db, settings) if s.domain != site.domain]
            await client.apply(caddy.build_config(specs))

        async def do_pool() -> None:
            await php_fpm.remove_pool(site.site_user, site.php_version, settings)

        async def do_database() -> None:
            # No-op for plain sites; idempotent DROP IF EXISTS otherwise.
            if site.wp_db_name and site.wp_db_user:
                await mariadb.drop_database(site.wp_db_name, site.wp_db_user)

        async def do_docroot() -> None:
            await fs.remove_tree(site_dir, root=settings.sites_root)

        async def do_user() -> None:
            await users.delete(site.site_user)

        async def do_finalize() -> None:
            await db.delete(site)
            await db.commit()

        ok, error = await _run_pipeline(
            db,
            op,
            [
                _Step("caddy", do_caddy, None),
                _Step("php_pool", do_pool, None),
                _Step("database", do_database, None),
                _Step("doc_root", do_docroot, None),
                _Step("linux_user", do_user, None),
                _Step("finalize", do_finalize, None),
            ],
        )
        if ok:
            await _finish(db, op, status="succeeded")
        else:
            site.status = "error"
            site.error_message = error
            await _finish(db, op, status="failed", error=error)


# --- post-provisioning site changes (Week 11) ----------------------------------


async def change_php_version(
    db: AsyncSession,
    settings: Settings,
    site: Site,
    new_version: str,
    *,
    caddy_client: CaddyClient | None = None,
) -> None:
    """Zero-downtime PHP switch: new pool up → Caddy repointed → old pool gone."""
    old_version = site.php_version
    if new_version == old_version:
        return
    client = _caddy_client(settings, caddy_client)
    await php_fpm.install_pool(
        site.site_user,
        new_version,
        settings,
        memory_limit=site.php_memory_limit,
        upload_max_filesize=site.php_upload_max_filesize,
    )
    site.php_version = new_version
    try:
        await client.apply(caddy.build_config(await _served_specs(db, settings)))
    except Exception:
        # Caddy still points at the old socket; drop the new pool and bail.
        site.php_version = old_version
        await php_fpm.remove_pool(site.site_user, new_version, settings)
        raise
    await php_fpm.remove_pool(site.site_user, old_version, settings)
    await db.commit()


async def update_php_settings(
    settings: Settings,
    site: Site,
    db: AsyncSession,
    *,
    memory_limit: str,
    upload_max_filesize: str,
) -> None:
    """Rewrite the pool with new limits and reload. Caddy is unaffected."""
    php_fpm.validate_php_size(memory_limit, name="memory_limit")
    php_fpm.validate_php_size(upload_max_filesize, name="upload_max_filesize")
    await php_fpm.install_pool(
        site.site_user,
        site.php_version,
        settings,
        memory_limit=memory_limit,
        upload_max_filesize=upload_max_filesize,
    )
    site.php_memory_limit = memory_limit
    site.php_upload_max_filesize = upload_max_filesize
    await db.commit()
