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
from app.db.models import App, Database, Operation, Site, User
from app.services import caddy, filebrowser, mariadb, php_fpm
from app.services import dns as dns_service
from app.services.caddy import CaddyClient, SiteSpec
from app.system import fs, slices, users

log = structlog.get_logger("hosty.sites")

LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

INDEX_SKELETON = """\
<?php
http_response_code(200);
?>
<!doctype html>
<title>{domain}</title>
<h1>{domain}</h1>
<p>This site was just created with HostyPanel. Replace
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


def spec_for(site: Site, settings: Settings, *, suspended: bool = False) -> SiteSpec:
    return SiteSpec(
        domain=site.domain,
        doc_root=site.doc_root,
        php_socket=php_fpm.socket_path(site.site_user, site.php_version, settings),
        internal_tls=site.behind_cloudflare,
        suspended=suspended,
    )


def _panel_spec(settings: Settings) -> caddy.PanelSpec | None:
    if not settings.panel_domain:
        return None
    return caddy.PanelSpec(
        domain=settings.panel_domain,
        upstream=settings.panel_upstream,
        allowed_ips=tuple(settings.panel_allowed_ips),
    )


async def build_full_config(
    db: AsyncSession, settings: Settings, *, exclude_domain: str | None = None
) -> dict:
    """The complete desired-state Caddy config for the current panel state."""
    from app.services import stacks as stacks_service

    specs = await _served_specs(db, settings)
    app_specs = await _served_app_specs(db)
    stack_routes = await stacks_service.stack_routes(db, settings)
    if exclude_domain is not None:
        specs = [s for s in specs if s.domain != exclude_domain]
        app_specs = [a for a in app_specs if a.domain != exclude_domain]
        stack_routes = [r for r in stack_routes if r.domain != exclude_domain]
    return caddy.build_config(
        specs,
        apps=app_specs,
        stacks=stack_routes,
        adminer=_adminer_spec(settings),
        panel=_panel_spec(settings),
        tls_internal=settings.caddy_tls_internal,
        access_log_path=settings.caddy_access_log_path or None,
    )


def _adminer_spec(settings: Settings) -> caddy.AdminerSpec | None:
    if not settings.adminer_enabled:
        return None
    from app.services.adminer import ADMINER_SOCKET

    return caddy.AdminerSpec(
        listen_addr=settings.adminer_internal_addr,
        root=settings.adminer_root,
        php_socket=ADMINER_SOCKET,
    )


async def _served_specs(db: AsyncSession, settings: Settings) -> list[SiteSpec]:
    """Specs for every site that should currently be served by Caddy.

    Sites whose owner is suspended stay in the config but answer 503 — taking
    them offline with a clear page, not a connection error (Phase 11d).
    """
    rows = (
        await db.execute(
            select(Site, User.suspended)
            .join(User, User.id == Site.owner_id, isouter=True)
            .where(Site.status.in_(("active", "provisioning")))
        )
    ).all()
    return [spec_for(site, settings, suspended=bool(suspended)) for site, suspended in rows]


async def _served_app_specs(db: AsyncSession) -> list[caddy.AppSpec]:
    """Phase 12a: vhosts for containerized apps. Stopped/errored apps keep
    their route (Caddy answers 502, which is honest); suspension 503s exactly
    like sites. Lives here so apps.py can depend on sites.py one-way."""
    rows = (
        await db.execute(
            select(App, User.suspended)
            .join(User, User.id == App.owner_id, isouter=True)
            .where(App.status.in_(("provisioning", "running", "stopped", "error")))
        )
    ).all()
    return [
        caddy.AppSpec(
            domain=app.domain,
            upstream=f"127.0.0.1:{app.host_port}",
            suspended=bool(suspended),
        )
        for app, suspended in rows
    ]


# --- operation step tracking -------------------------------------------------

CREATE_STEPS = [
    ("linux_user", "Create isolated Linux user"),
    ("doc_root", "Create web root and skeleton"),
    ("php_pool", "Configure PHP-FPM pool"),
    ("caddy", "Publish vhost to Caddy"),
    ("filebrowser", "Register file manager access"),
    ("finalize", "Activate site"),
]

DNS_ZONE_STEP = ("dns_zone", "Create DNS zone")


def create_steps(*, with_dns_zone: bool = False) -> list[tuple[str, str]]:
    """CREATE_STEPS, optionally with the Week 17 auto-create-zone step."""
    steps = list(CREATE_STEPS)
    if with_dns_zone:
        steps.insert(-1, DNS_ZONE_STEP)  # after filebrowser, before finalize
    return steps


DELETE_STEPS = [
    ("caddy", "Remove vhost from Caddy"),
    ("filebrowser", "Remove file manager access"),
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
    create_dns_zone: bool = False,
    pdns_client: dns_service.PowerDNSClient | None = None,
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
            # Phase 11c: outer cgroup cap from the owner's effective limits.
            # Best-effort by design — never blocks provisioning.
            from app.services import quotas

            owner = await db.get(User, site.owner_id) if site.owner_id else None
            limits = quotas.UNLIMITED if owner is None else await quotas.effective_limits(db, owner)
            await slices.install_slice(
                site.site_user,
                cpu_quota_percent=limits.cpu_quota_percent,
                memory_max_mb=limits.memory_max_mb,
            )

        async def undo_user() -> None:
            await slices.remove_slice(site.site_user)
            await users.delete(site.site_user)

        async def do_docroot() -> None:
            await fs.secure_site_layout(
                site_dir, site.doc_root, site.site_user, root=settings.sites_root
            )
            fs.write_file(
                f"{site.doc_root}/index.php",
                INDEX_SKELETON.format(domain=site.domain),
                root=settings.sites_root,
            )
            await fs.secure_site_layout(
                site_dir, site.doc_root, site.site_user, root=settings.sites_root, create=False
            )

        async def undo_docroot() -> None:
            await fs.remove_tree(site_dir, root=settings.sites_root)

        async def do_pool() -> None:
            await php_fpm.install_pool(
                site.site_user,
                site.php_version,
                settings,
                doc_root=site.doc_root,
                memory_limit=site.php_memory_limit,
                upload_max_filesize=site.php_upload_max_filesize,
            )

        async def undo_pool() -> None:
            await php_fpm.remove_pool(site.site_user, site.php_version, settings)

        async def do_caddy() -> None:
            await client.apply(await build_full_config(db, settings))

        async def undo_caddy() -> None:
            await client.apply(await build_full_config(db, settings, exclude_domain=site.domain))

        async def do_filebrowser() -> None:
            await filebrowser.ensure_site_user(site.site_user, site.domain, settings)

        async def undo_filebrowser() -> None:
            await filebrowser.remove_site_user(site.site_user, settings)

        # Week 17: optionally auto-create the PowerDNS zone (SOA/NS + A -> server).
        zone_created = False

        def _pdns() -> dns_service.PowerDNSClient:
            return pdns_client or dns_service.PowerDNSClient(
                settings.pdns_api_url, settings.pdns_api_key, settings.pdns_server_id
            )

        async def do_dns_zone() -> None:
            nonlocal zone_created
            zone_created = await dns_service.create_zone_with_defaults(
                _pdns(),
                site.domain,
                nameservers=settings.dns_nameservers,
                public_ip=settings.public_ip,
                ttl=settings.dns_default_ttl,
            )
            if zone_created and site.owner_id is not None:
                # Phase 11b: the auto-created zone belongs to the site's owner.
                from app.db.models import DnsZoneOwner

                db.add(
                    DnsZoneOwner(zone=dns_service.canonical(site.domain), owner_id=site.owner_id)
                )
                await db.commit()

        async def undo_dns_zone() -> None:
            if zone_created:  # never delete a zone that existed before us
                await _pdns().delete_zone(dns_service.canonical(site.domain))
                from app.db.models import DnsZoneOwner

                row = (
                    await db.execute(
                        select(DnsZoneOwner).where(
                            DnsZoneOwner.zone == dns_service.canonical(site.domain)
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    await db.delete(row)
                    await db.commit()

        async def do_finalize() -> None:
            site.status = "active"
            op.site_id = site.id
            await db.commit()

        steps = [
            _Step("linux_user", do_user, undo_user),
            _Step("doc_root", do_docroot, undo_docroot),
            _Step("php_pool", do_pool, undo_pool),
            _Step("caddy", do_caddy, undo_caddy),
            _Step("filebrowser", do_filebrowser, undo_filebrowser),
        ]
        if create_dns_zone:
            steps.append(_Step("dns_zone", do_dns_zone, undo_dns_zone))
        steps.append(_Step("finalize", do_finalize, None))

        ok, error = await _run_pipeline(db, op, steps)
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
            await client.apply(await build_full_config(db, settings, exclude_domain=site.domain))

        async def do_filebrowser() -> None:
            await filebrowser.remove_site_user(site.site_user, settings)

        async def do_pool() -> None:
            await php_fpm.remove_pool(site.site_user, site.php_version, settings)

        async def do_database() -> None:
            # Idempotent DROP IF EXISTS for every panel-managed database.
            rows = (
                (await db.execute(select(Database).where(Database.site_id == site.id)))
                .scalars()
                .all()
            )
            for row in rows:
                await mariadb.drop_database(row.name, row.db_user)
            if site.wp_db_name and site.wp_db_user:  # legacy safety net
                await mariadb.drop_database(site.wp_db_name, site.wp_db_user)

        async def do_docroot() -> None:
            await fs.remove_tree(site_dir, root=settings.sites_root)

        async def do_user() -> None:
            await slices.remove_slice(site.site_user)
            await users.delete(site.site_user)

        async def do_finalize() -> None:
            await db.delete(site)
            await db.commit()

        ok, error = await _run_pipeline(
            db,
            op,
            [
                _Step("caddy", do_caddy, None),
                _Step("filebrowser", do_filebrowser, None),
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


async def resync_caddy(
    db: AsyncSession,
    settings: Settings,
    *,
    caddy_client: CaddyClient | None = None,
) -> None:
    """Re-apply the full desired-state Caddy config.

    Re-applying nudges Caddy to (re)attempt ACME issuance for any domain that
    does not yet have a valid certificate — e.g. after the user fixes DNS.
    Caddy renews valid certificates automatically; this is for retrying, not
    for routine renewal.
    """
    client = _caddy_client(settings, caddy_client)
    await client.apply(await build_full_config(db, settings))


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
        doc_root=site.doc_root,
        memory_limit=site.php_memory_limit,
        upload_max_filesize=site.php_upload_max_filesize,
    )
    site.php_version = new_version
    try:
        await client.apply(await build_full_config(db, settings))
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
        doc_root=site.doc_root,
        memory_limit=memory_limit,
        upload_max_filesize=upload_max_filesize,
    )
    site.php_memory_limit = memory_limit
    site.php_upload_max_filesize = upload_max_filesize
    await db.commit()
