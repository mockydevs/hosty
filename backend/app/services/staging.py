"""Staging clones (Phase 11d): copy a site + DB to staging.<domain>, push back.

Create reuses the site-provisioning building blocks (Linux user, PHP pool,
Caddy vhost, Filebrowser) as a transactional pipeline with rollback, then
mirrors the files and clones the WordPress database (dump → restore →
`wp config set` → `wp search-replace`). Push-back mirrors files in reverse
(with --delete) and replays the staging DB into production — destructive on
purpose, behind a type-the-domain confirmation in the API.
"""

from __future__ import annotations

import os
import tempfile

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import hash_token
from app.db.models import Database, Operation, Site, User
from app.services import backup, filebrowser, mariadb, php_fpm, wordpress
from app.services.sites import (
    _caddy_client,
    _finish,
    _run_pipeline,
    _Step,
    build_full_config,
    derive_site_user,
    doc_root_for,
    site_dir_for,
)
from app.system import fs, slices, users

log = structlog.get_logger("hosty.staging")

STAGING_PREFIX = "staging."


class StagingError(RuntimeError):
    pass


def staging_domain_for(domain: str) -> str:
    staging = f"{STAGING_PREFIX}{domain}"
    if len(staging) > 253:
        raise StagingError("Domain too long to derive a staging.<domain> name")
    return staging


CREATE_STAGING_STEPS = [
    ("linux_user", "Create isolated Linux user"),
    ("doc_root", "Copy site files"),
    ("php_pool", "Configure PHP-FPM pool"),
    ("database", "Clone database"),
    ("caddy", "Publish staging vhost"),
    ("filebrowser", "Register file manager access"),
    ("finalize", "Activate staging site"),
]

PUSH_STAGING_STEPS = [
    ("files", "Mirror staging files to production"),
    ("database", "Replay staging database into production"),
    ("search_replace", "Rewrite staging domain to production"),
    ("finalize", "Finish"),
]


async def run_create_staging(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    source_site_id: int,
    staging_site_id: int,
    operation_id: int,
    caddy_client=None,
) -> None:
    async with sessionmaker() as db:
        source = await db.get(Site, source_site_id)
        site = await db.get(Site, staging_site_id)
        op = await db.get(Operation, operation_id)
        assert source is not None and site is not None and op is not None
        op.status = "running"
        await db.commit()

        client = _caddy_client(settings, caddy_client)
        site_dir = site_dir_for(site.domain, settings)
        db_password = mariadb.generate_password()
        db_name, db_user = mariadb.db_identifiers_for(site.site_user)

        async def do_user() -> None:
            await users.create(site.site_user)
            if site.owner_id is not None:
                from app.services import quotas

                owner = await db.get(User, site.owner_id)
                limits = (
                    quotas.UNLIMITED if owner is None else await quotas.effective_limits(db, owner)
                )
                await slices.install_slice(
                    site.site_user,
                    cpu_quota_percent=limits.cpu_quota_percent,
                    memory_max_mb=limits.memory_max_mb,
                )

        async def undo_user() -> None:
            await slices.remove_slice(site.site_user)
            await users.delete(site.site_user)

        async def do_docroot() -> None:
            await fs.create_dir(site.doc_root, root=settings.sites_root)
            await fs.mirror_tree(source.doc_root, site.doc_root, root=settings.sites_root)
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

        async def do_database() -> None:
            if not source.wordpress or not source.wp_db_name:
                return  # nothing to clone for plain sites
            await mariadb.create_database(db_name, db_user, db_password)
            with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as tmp:
                dump_path = tmp.name
            try:
                result = await runner_dump(source.wp_db_name, dump_path)
                if not result.ok:
                    raise StagingError(f"dump failed: {result.stderr.strip()[:300]}")
                result = await runner_restore(db_name, dump_path)
                if not result.ok:
                    raise StagingError(f"restore failed: {result.stderr.strip()[:300]}")
            finally:
                if os.path.exists(dump_path):
                    os.unlink(dump_path)
            # Point the cloned wp-config at the staging DB, then rewrite URLs.
            for key, value in (
                ("DB_NAME", db_name),
                ("DB_USER", db_user),
                ("DB_PASSWORD", db_password),
            ):
                await wordpress.run_wp(site.site_user, site.doc_root, ["config", "set", key, value])
            await wordpress.run_wp(
                site.site_user,
                site.doc_root,
                ["search-replace", source.domain, site.domain, "--all-tables", "--precise"],
                timeout=600,
            )

        async def undo_database() -> None:
            if source.wordpress and source.wp_db_name:
                await mariadb.drop_database(db_name, db_user)

        async def do_caddy() -> None:
            await client.apply(await build_full_config(db, settings))

        async def undo_caddy() -> None:
            await client.apply(await build_full_config(db, settings, exclude_domain=site.domain))

        async def do_filebrowser() -> None:
            await filebrowser.ensure_site_user(site.site_user, site.domain, settings)

        async def undo_filebrowser() -> None:
            await filebrowser.remove_site_user(site.site_user, settings)

        async def do_finalize() -> None:
            site.status = "active"
            if source.wordpress and source.wp_db_name:
                site.wordpress = True
                site.wp_db_name = db_name
                site.wp_db_user = db_user
                db.add(
                    Database(
                        site_id=site.id,
                        name=db_name,
                        db_user=db_user,
                        purpose="wordpress",
                        password_hash=hash_token(db_password),
                    )
                )
            op.site_id = site.id
            await db.commit()

        ok, error = await _run_pipeline(
            db,
            op,
            [
                _Step("linux_user", do_user, undo_user),
                _Step("doc_root", do_docroot, undo_docroot),
                _Step("php_pool", do_pool, undo_pool),
                _Step("database", do_database, undo_database),
                _Step("caddy", do_caddy, undo_caddy),
                _Step("filebrowser", do_filebrowser, undo_filebrowser),
                _Step("finalize", do_finalize, None),
            ],
        )
        if ok:
            await _finish(db, op, status="succeeded")
        else:
            site.status = "error"
            site.error_message = error
            await _finish(db, op, status="failed", error=error)


async def run_push_staging(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    staging_site_id: int,
    operation_id: int,
) -> None:
    """Push staging back to production: files (with --delete), DB, URLs."""
    async with sessionmaker() as db:
        staging_site = await db.get(Site, staging_site_id)
        op = await db.get(Operation, operation_id)
        assert staging_site is not None and op is not None
        assert staging_site.staging_of is not None
        production = await db.get(Site, staging_site.staging_of)
        assert production is not None
        op.status = "running"
        await db.commit()

        async def do_files() -> None:
            await fs.mirror_tree(
                staging_site.doc_root, production.doc_root, root=settings.sites_root, delete=True
            )
            await fs.chown_recursive(
                production.site_user,
                site_dir_for(production.domain, settings),
                root=settings.sites_root,
            )

        async def do_database() -> None:
            if not (staging_site.wp_db_name and production.wp_db_name):
                return
            with tempfile.NamedTemporaryFile(suffix=".sql", delete=False) as tmp:
                dump_path = tmp.name
            try:
                result = await runner_dump(staging_site.wp_db_name, dump_path)
                if not result.ok:
                    raise StagingError(f"dump failed: {result.stderr.strip()[:300]}")
                result = await runner_restore(production.wp_db_name, dump_path)
                if not result.ok:
                    raise StagingError(f"restore failed: {result.stderr.strip()[:300]}")
            finally:
                if os.path.exists(dump_path):
                    os.unlink(dump_path)

        async def do_search_replace() -> None:
            # The mirrored files carry the staging wp-config — point production's
            # copy back at its own DB, then rewrite URLs inside that DB.
            if production.wordpress and production.wp_db_name:
                # The mirrored wp-config carries the STAGING DB credentials. The
                # production password is never stored in plaintext, so rotate it
                # and write the fresh one into wp-config.
                prod_db_user = production.wp_db_user or production.wp_db_name
                new_password = mariadb.generate_password()
                await mariadb.reset_password(prod_db_user, new_password)
                row = (
                    await db.execute(
                        select(Database).where(
                            Database.site_id == production.id,
                            Database.name == production.wp_db_name,
                        )
                    )
                ).scalar_one_or_none()
                if row is not None:
                    row.password_hash = hash_token(new_password)
                    await db.commit()
                for key, value in (
                    ("DB_NAME", production.wp_db_name),
                    ("DB_USER", prod_db_user),
                    ("DB_PASSWORD", new_password),
                ):
                    await wordpress.run_wp(
                        production.site_user, production.doc_root, ["config", "set", key, value]
                    )
                await wordpress.run_wp(
                    production.site_user,
                    production.doc_root,
                    [
                        "search-replace",
                        staging_site.domain,
                        production.domain,
                        "--all-tables",
                        "--precise",
                    ],
                    timeout=600,
                )

        async def do_finalize() -> None:
            await db.commit()

        ok, error = await _run_pipeline(
            db,
            op,
            [
                _Step("files", do_files, None),
                _Step("database", do_database, None),
                _Step("search_replace", do_search_replace, None),
                _Step("finalize", do_finalize, None),
            ],
        )
        await _finish(db, op, status="succeeded" if ok else "failed", error=error)


# Thin wrappers so tests can monkeypatch dump/restore in one place.
async def runner_dump(db_name: str, dump_path: str):
    from app.system import runner

    return await runner.run(
        backup.build_mysqldump_argv(db_name), timeout=1800, stdout_path=dump_path
    )


async def runner_restore(db_name: str, dump_path: str):
    from app.system import runner

    return await runner.run(
        backup.build_mysql_restore_argv(db_name), timeout=1800, stdin_path=dump_path
    )


def make_staging_site(source: Site, settings: Settings) -> Site:
    """The staging Site row (not yet persisted)."""
    staging = staging_domain_for(source.domain)
    return Site(
        owner_id=source.owner_id,
        domain=staging,
        site_user=derive_site_user(staging),
        doc_root=doc_root_for(staging, settings),
        php_version=source.php_version,
        status="provisioning",
        php_memory_limit=source.php_memory_limit,
        php_upload_max_filesize=source.php_upload_max_filesize,
        staging_of=source.id,
    )
