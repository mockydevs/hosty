"""Site migration / import (Phase 11d).

The panel-friendly variant of "rsync files in": the user uploads a files
archive (tar.gz/tgz/zip) and/or an SQL dump; the pipeline extracts the files
into the doc root (archive members are validated against path traversal
before anything is written), imports the dump into the site's database, and —
for WordPress sites — runs `wp search-replace old-domain new-domain`.
"""

from __future__ import annotations

import os
import tarfile
import zipfile

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import Database, Operation, Site
from app.services import backup, wordpress
from app.system import fs, runner

log = structlog.get_logger("hosty.site_import")

ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".zip")


class ImportError_(RuntimeError):
    pass


def import_steps(*, with_files: bool, with_sql: bool, with_replace: bool) -> list[tuple[str, str]]:
    steps = []
    if with_files:
        steps.append(("files", "Extract uploaded files into the web root"))
    if with_sql:
        steps.append(("database", "Import SQL dump"))
    if with_replace:
        steps.append(("search_replace", "Rewrite old domain (wp search-replace)"))
    steps.append(("finalize", "Clean up"))
    return steps


def _validate_member_name(name: str) -> str:
    """Reject absolute paths and traversal in archive member names."""
    if name.startswith("/") or name.startswith("\\"):
        raise ImportError_(f"Archive contains an absolute path: {name!r}")
    parts = name.replace("\\", "/").split("/")
    if ".." in parts:
        raise ImportError_(f"Archive contains a path traversal: {name!r}")
    return name


def extract_archive(archive_path: str, dest: str) -> int:
    """Extract a validated archive into `dest`; returns the member count.

    tarfile uses the Python 3.12 'data' filter (strips setuid, device nodes,
    absolute names); zip members are validated by hand.
    """
    count = 0
    if archive_path.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive_path, "r:gz") as tar:
            for member in tar.getmembers():
                _validate_member_name(member.name)
            tar.extractall(dest, filter="data")
            count = len(tar.getmembers())
    elif archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                _validate_member_name(info.filename)
            zf.extractall(dest)
            count = len(zf.infolist())
    else:
        raise ImportError_(f"Unsupported archive type: {os.path.basename(archive_path)!r}")
    return count


async def run_import_site(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    site_id: int,
    operation_id: int,
    archive_path: str | None,
    sql_path: str | None,
    old_domain: str | None,
    target_db: str | None,
) -> None:
    """Background entrypoint. No compensation (like restore, it overwrites in
    place by design) — the user is told to take a backup first in the UI."""
    from app.services.sites import _finish, _run_pipeline, _Step

    async with sessionmaker() as db:
        site = await db.get(Site, site_id)
        op = await db.get(Operation, operation_id)
        assert site is not None and op is not None
        op.status = "running"
        await db.commit()

        async def do_files() -> None:
            assert archive_path is not None
            count = extract_archive(archive_path, site.doc_root)
            await fs.chown_recursive(site.site_user, site.doc_root, root=settings.sites_root)
            log.info("import_files_extracted", domain=site.domain, members=count)

        async def do_database() -> None:
            assert sql_path is not None and target_db is not None
            row = (
                await db.execute(
                    select(Database).where(Database.site_id == site.id, Database.name == target_db)
                )
            ).scalar_one_or_none()
            if row is None:
                raise ImportError_(f"Database {target_db!r} does not belong to this site")
            result = await runner.run(
                backup.build_mysql_restore_argv(row.name),
                timeout=1800,
                stdin_path=sql_path,
            )
            if not result.ok:
                raise ImportError_(f"SQL import failed: {result.stderr.strip()[:300]}")

        async def do_search_replace() -> None:
            assert old_domain is not None
            await wordpress.run_wp(
                site.site_user,
                site.doc_root,
                ["search-replace", old_domain, site.domain, "--all-tables", "--precise"],
                timeout=600,
            )

        async def do_finalize() -> None:
            for path in (archive_path, sql_path):
                if path and os.path.exists(path):
                    os.unlink(path)

        steps: list[_Step] = []
        if archive_path:
            steps.append(_Step("files", do_files, None))
        if sql_path:
            steps.append(_Step("database", do_database, None))
        if old_domain and site.wordpress:
            steps.append(_Step("search_replace", do_search_replace, None))
        steps.append(_Step("finalize", do_finalize, None))

        ok, error = await _run_pipeline(db, op, steps)
        await _finish(db, op, status="succeeded" if ok else "failed", error=error)
        log.info("import_finished", domain=site.domain, ok=ok)
