"""WordPress management via WP-CLI (Week 12-13).

Every WP-CLI invocation runs AS THE SITE'S LINUX USER through `runuser`
(never root), against the site's doc root. Argv builders are pure and
unit-tested. Core downloads share a cache directory so repeat installs are
fast. The install itself is a transactional pipeline with rollback.
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.security import hash_token
from app.db.models import Database, Operation, Site
from app.services import mariadb
from app.system import fs, runner
from app.system.users import validate_site_username

log = structlog.get_logger("hosty.wordpress")

WP_CLI_CACHE_DIR = "/var/cache/hosty/wp-cli"
LOCALE_RE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")
WP_VERSION_RE = re.compile(r"^(latest|[0-9]+\.[0-9]+(\.[0-9]+)?)$")

WP_ACTIONS = frozenset(
    {"update_core", "maintenance_on", "maintenance_off", "shuffle_salts", "login_link"}
)


class WordPressError(RuntimeError):
    pass


class InvalidWpArgumentError(ValueError):
    pass


def validate_locale(locale: str) -> str:
    if not isinstance(locale, str) or not LOCALE_RE.fullmatch(locale):
        raise InvalidWpArgumentError(f"Invalid locale: {locale!r} (e.g. en_US, de_DE)")
    return locale


def validate_wp_version(version: str) -> str:
    if not isinstance(version, str) or not WP_VERSION_RE.fullmatch(version):
        raise InvalidWpArgumentError(f"Invalid WordPress version: {version!r}")
    return version


def build_wp_argv(site_user: str, doc_root: str, args: list[str]) -> list[str]:
    """Pure: `runuser -u <site-user> -- wp <args> --path=<doc_root>`."""
    validate_site_username(site_user)
    for item in args:
        if not isinstance(item, str) or "\x00" in item:
            raise InvalidWpArgumentError(f"Invalid WP-CLI argument: {item!r}")
    return ["runuser", "-u", site_user, "--", "wp", *args, f"--path={doc_root}"]


async def run_wp(
    site_user: str,
    doc_root: str,
    args: list[str],
    *,
    timeout: float = 120.0,
    check: bool = True,
) -> runner.CommandResult:
    result = await runner.run(
        build_wp_argv(site_user, doc_root, args),
        timeout=timeout,
        env={"WP_CLI_CACHE_DIR": WP_CLI_CACHE_DIR, "HOME": f"/home/{site_user}"},
    )
    if check and not result.ok:
        message = (result.stderr.strip() or result.stdout.strip())[:300]
        raise WordPressError(f"wp {args[0] if args else '?'} failed: {message}")
    return result


async def is_installed(site: Site) -> bool:
    result = await run_wp(
        site.site_user, site.doc_root, ["core", "is-installed"], timeout=30, check=False
    )
    return result.ok


# --- one-click install pipeline -------------------------------------------------


@dataclass(frozen=True)
class InstallParams:
    title: str
    admin_user: str
    admin_password: str
    admin_email: str
    locale: str = "en_US"
    version: str = "latest"


INSTALL_STEPS = [
    ("database", "Provision MariaDB database"),
    ("download", "Download WordPress core"),
    ("configure", "Write wp-config.php"),
    ("install", "Run the WordPress installer"),
    ("finalize", "Record install"),
]


async def run_install_wordpress(
    sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    site_id: int,
    operation_id: int,
    params: InstallParams,
) -> None:
    """Background entrypoint mirroring the site pipeline: any failed step rolls
    back everything done so far (DB dropped, files restored to the skeleton)."""
    from app.services.sites import INDEX_SKELETON, _finish, _run_pipeline, _Step

    async with sessionmaker() as db:
        site = await db.get(Site, site_id)
        op = await db.get(Operation, operation_id)
        assert site is not None and op is not None
        op.status = "running"
        await db.commit()

        db_name, db_user = mariadb.db_identifiers_for(site.site_user)
        db_password = mariadb.generate_password()

        async def do_database() -> None:
            await mariadb.create_database(db_name, db_user, db_password)

        async def undo_database() -> None:
            await mariadb.drop_database(db_name, db_user)

        async def do_download() -> None:
            args = ["core", "download", f"--locale={params.locale}", "--skip-content=false"]
            if params.version != "latest":
                args.append(f"--version={params.version}")
            # The skeleton index.php would make `wp core download` refuse: --force
            # overwrites it (we recreate it on rollback).
            args.append("--force")
            await run_wp(site.site_user, site.doc_root, args, timeout=300)

        async def undo_download() -> None:
            # Restore the doc root to the pristine post-provisioning skeleton.
            await fs.remove_tree(site.doc_root, root=settings.sites_root)
            await fs.create_dir(site.doc_root, root=settings.sites_root)
            fs.write_file(
                f"{site.doc_root}/index.php",
                INDEX_SKELETON.format(domain=site.domain),
                root=settings.sites_root,
            )
            await fs.chown_recursive(site.site_user, site.doc_root, root=settings.sites_root)

        async def do_configure() -> None:
            await run_wp(
                site.site_user,
                site.doc_root,
                [
                    "config",
                    "create",
                    f"--dbname={db_name}",
                    f"--dbuser={db_user}",
                    f"--dbpass={db_password}",
                    "--dbhost=localhost",
                    "--force",
                ],
            )

        async def do_install() -> None:
            await run_wp(
                site.site_user,
                site.doc_root,
                [
                    "core",
                    "install",
                    f"--url=https://{site.domain}",
                    f"--title={params.title}",
                    f"--admin_user={params.admin_user}",
                    f"--admin_password={params.admin_password}",
                    f"--admin_email={params.admin_email}",
                    "--skip-email",
                ],
                timeout=300,
            )

        async def do_finalize() -> None:
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
            await db.commit()

        ok, error = await _run_pipeline(
            db,
            op,
            [
                _Step("database", do_database, undo_database),
                _Step("download", do_download, undo_download),
                _Step("configure", do_configure, None),  # files undone by download step
                _Step("install", do_install, None),  # DB content undone by database step
                _Step("finalize", do_finalize, None),
            ],
        )
        await _finish(db, op, status="succeeded" if ok else "failed", error=error)


# --- status & actions (Week 13) --------------------------------------------------


@dataclass(frozen=True)
class WpStatus:
    installed: bool
    version: str | None = None
    update_available: str | None = None
    plugin_count: int | None = None
    theme_count: int | None = None


async def status(site: Site) -> WpStatus:
    if not await is_installed(site):
        return WpStatus(installed=False)

    async def out(args: list[str], timeout: float = 60) -> str | None:
        result = await run_wp(site.site_user, site.doc_root, args, timeout=timeout, check=False)
        return result.stdout.strip() if result.ok else None

    version = await out(["core", "version"])
    update_available = None
    updates_json = await out(["core", "check-update", "--format=json"], timeout=60)
    if updates_json:
        try:
            updates = json.loads(updates_json)
            if updates:
                update_available = updates[0].get("version")
        except (json.JSONDecodeError, AttributeError, IndexError):
            update_available = None

    def count(raw: str | None) -> int | None:
        try:
            return int(raw) if raw is not None and raw.isdigit() else None
        except ValueError:
            return None

    plugin_count = count(await out(["plugin", "list", "--format=count"]))
    theme_count = count(await out(["theme", "list", "--format=count"]))
    return WpStatus(
        installed=True,
        version=version,
        update_available=update_available,
        plugin_count=plugin_count,
        theme_count=theme_count,
    )


async def run_action(site: Site, action: str) -> str | None:
    """Execute a management action; returns a URL for `login_link`, else None."""
    if action not in WP_ACTIONS:
        raise InvalidWpArgumentError(f"Unknown WordPress action: {action!r}")
    if action == "update_core":
        await run_wp(site.site_user, site.doc_root, ["core", "update"], timeout=300)
        await run_wp(site.site_user, site.doc_root, ["core", "update-db"], timeout=120)
        return None
    if action == "maintenance_on":
        await run_wp(site.site_user, site.doc_root, ["maintenance-mode", "activate"])
        return None
    if action == "maintenance_off":
        await run_wp(site.site_user, site.doc_root, ["maintenance-mode", "deactivate"])
        return None
    if action == "shuffle_salts":
        await run_wp(site.site_user, site.doc_root, ["config", "shuffle-salts"])
        return None
    # login_link: one-time magic login URL via the wp-cli login command package
    # (aaemnnosttv/wp-cli-login-command); installed on first use.
    probe = await run_wp(site.site_user, site.doc_root, ["help", "login"], check=False)
    if not probe.ok:
        await run_wp(
            site.site_user,
            site.doc_root,
            ["package", "install", "aaemnnosttv/wp-cli-login-command"],
            timeout=300,
        )
        await run_wp(site.site_user, site.doc_root, ["login", "install", "--activate", "--yes"])
    admins = await run_wp(
        site.site_user,
        site.doc_root,
        ["user", "list", "--role=administrator", "--field=user_login"],
    )
    admin = admins.stdout.strip().splitlines()[0] if admins.stdout.strip() else None
    if not admin:
        raise WordPressError("No administrator user found")
    result = await run_wp(site.site_user, site.doc_root, ["login", "create", admin, "--url-only"])
    return result.stdout.strip() or None


def generate_admin_password() -> str:
    return secrets.token_urlsafe(18)
