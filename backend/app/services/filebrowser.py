"""Filebrowser integration (Week 16, ADR-008).

One Filebrowser instance runs on an internal-only listener with PROXY auth:
it trusts a username header that ONLY the panel's authenticated /files proxy
can set. Each site gets a Filebrowser user scoped to its own directory, so
site A's session can never browse site B — Filebrowser enforces the scope,
the panel enforces who the session is.
"""

from __future__ import annotations

import secrets

import structlog

from app.core.config import Settings
from app.system import runner
from app.system.users import validate_site_username

log = structlog.get_logger("hosty.filebrowser")

AUTH_HEADER = "X-Hosty-Fb-User"


class FilebrowserError(RuntimeError):
    pass


def _base_argv(settings: Settings) -> list[str]:
    return ["filebrowser", "-d", settings.filebrowser_db]


def build_config_init_argv(settings: Settings) -> list[str]:
    """Bootstrap config: proxy auth + internal listener + /var/www root."""
    host, _, port = settings.filebrowser_internal_addr.partition(":")
    return [
        *_base_argv(settings),
        "config",
        "init",
        "--auth.method=proxy",
        f"--auth.header={AUTH_HEADER}",
        f"--root={settings.sites_root}",
        f"--address={host}",
        f"--port={port or '8082'}",
        "--signup=false",
    ]


def build_user_add_argv(site_user: str, domain: str, settings: Settings) -> list[str]:
    """Scoped user: may only see /<domain> below the sites root."""
    validate_site_username(site_user)
    if "/" in domain or "\x00" in domain or domain in {"", ".", ".."}:
        raise FilebrowserError(f"Invalid scope domain: {domain!r}")
    # Random throwaway password: logins happen via proxy header, never password.
    password = secrets.token_urlsafe(24)
    return [
        *_base_argv(settings),
        "users",
        "add",
        site_user,
        password,
        f"--scope=/{domain}",
        "--lockPassword",
    ]


def build_user_rm_argv(site_user: str, settings: Settings) -> list[str]:
    return [*_base_argv(settings), "users", "rm", validate_site_username(site_user)]


async def ensure_site_user(site_user: str, domain: str, settings: Settings) -> bool:
    """Create the scoped Filebrowser user. Idempotent; returns False when
    Filebrowser is not installed (file management simply unavailable)."""
    try:
        result = await runner.run(build_user_add_argv(site_user, domain, settings), timeout=30)
    except runner.CommandNotFoundError:
        log.warning("filebrowser_missing_skipping", site_user=site_user)
        return False
    if not result.ok and "already exists" not in (result.stderr + result.stdout).lower():
        raise FilebrowserError(f"filebrowser users add failed: {result.stderr.strip()[:300]}")
    return True


async def remove_site_user(site_user: str, settings: Settings) -> bool:
    """Idempotent delete; False when Filebrowser is absent or user unknown."""
    try:
        result = await runner.run(build_user_rm_argv(site_user, settings), timeout=30)
    except runner.CommandNotFoundError:
        return False
    if not result.ok and "not found" not in (result.stderr + result.stdout).lower():
        raise FilebrowserError(f"filebrowser users rm failed: {result.stderr.strip()[:300]}")
    return result.ok
