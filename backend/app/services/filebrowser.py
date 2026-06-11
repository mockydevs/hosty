"""Filebrowser integration (Week 16, ADR-008).

One Filebrowser instance runs on an internal-only listener with PROXY auth:
it trusts a username header that ONLY the panel can set. Each site gets a
Filebrowser user scoped to its own directory, so site A's session can never
browse site B — Filebrowser enforces the scope, the panel enforces identity.

User management happens over Filebrowser's REST API (authenticated as the
provision-time `admin` user via the same proxy header), NOT the CLI: the
daemon holds an exclusive BoltDB lock, so any CLI call against the live
database deadlocks until timeout.

Two proxy-auth pitfalls this module defends against:
- Proxy auth AUTO-CREATES unknown users with the global default scope. The
  provisioning script pins that default to an empty quarantine directory so
  an unexpected auto-creation exposes nothing.
- The admin user must exist before the panel can manage anyone; provisioning
  creates it (password random + locked, never used — auth is the header).
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx
import structlog

from app.core.config import Settings
from app.system.users import validate_site_username

log = structlog.get_logger("hosty.filebrowser")

AUTH_HEADER = "X-Hosty-Fb-User"
QUARANTINE_SCOPE = "/.hosty-quarantine"

# Everything a site owner needs to manage their own files — nothing more.
SITE_USER_PERMS = {
    "admin": False,
    "execute": False,
    "create": True,
    "rename": True,
    "modify": True,
    "delete": True,
    "share": False,
    "download": True,
}


class FilebrowserError(RuntimeError):
    pass


def build_config_init_argv(settings: Settings) -> list[str]:
    """Bootstrap config: proxy auth + internal listener + quarantined defaults.

    Mirrored in installer/provision.sh; runs only while the daemon is stopped.
    """
    host, _, port = settings.filebrowser_internal_addr.partition(":")
    return [
        "filebrowser",
        "-d",
        settings.filebrowser_db,
        "config",
        "init",
        "--auth.method=proxy",
        f"--auth.header={AUTH_HEADER}",
        f"--root={settings.sites_root}",
        f"--scope={QUARANTINE_SCOPE}",
        # Served behind the panel's /files proxy: assets must resolve there.
        "--baseurl=/files",
        f"--address={host}",
        f"--port={port or '8082'}",
        "--signup=false",
    ]


def build_site_user_payload(site_user: str, domain: str) -> dict[str, Any]:
    """POST /api/users body for a site-scoped Filebrowser user."""
    validate_site_username(site_user)
    if "/" in domain or "\x00" in domain or domain in {"", ".", ".."}:
        raise FilebrowserError(f"Invalid scope domain: {domain!r}")
    return {
        "what": "user",
        "which": [],
        "data": {
            "username": site_user,
            # Random throwaway: logins happen via proxy header, never password.
            "password": secrets.token_urlsafe(24),
            "scope": f"/{domain}",
            "lockPassword": True,
            "perm": dict(SITE_USER_PERMS),
        },
    }


def _base_url(settings: Settings) -> str:
    return f"http://{settings.filebrowser_internal_addr}"


async def _admin_token(client: httpx.AsyncClient, settings: Settings) -> str:
    resp = await client.get(
        f"{_base_url(settings)}/api/login",
        headers={AUTH_HEADER: settings.filebrowser_admin_user},
    )
    if resp.status_code != 200:
        raise FilebrowserError(
            f"filebrowser admin login failed ({resp.status_code}): {resp.text[:200]}"
        )
    return resp.text.strip()


async def _list_users(
    client: httpx.AsyncClient, token: str, settings: Settings
) -> list[dict[str, Any]]:
    resp = await client.get(f"{_base_url(settings)}/api/users", headers={"X-Auth": token})
    if resp.status_code != 200:
        raise FilebrowserError(f"filebrowser users list failed ({resp.status_code})")
    return resp.json()


def _new_client() -> httpx.AsyncClient:
    # trust_env=False: internal-only listener; never route via HTTP(S) proxies.
    return httpx.AsyncClient(timeout=10.0, trust_env=False)


async def ensure_site_user(
    site_user: str,
    domain: str,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Create (or scope-correct) the site's Filebrowser user. Idempotent;
    returns False when Filebrowser is unreachable (file management simply
    unavailable on this server)."""
    payload = build_site_user_payload(site_user, domain)
    own = client is None
    client = client or _new_client()
    try:
        token = await _admin_token(client, settings)
        existing = next(
            (u for u in await _list_users(client, token, settings) if u["username"] == site_user),
            None,
        )
        if existing is None:
            resp = await client.post(
                f"{_base_url(settings)}/api/users", headers={"X-Auth": token}, json=payload
            )
            if resp.status_code not in (201, 409):
                raise FilebrowserError(
                    f"filebrowser user create failed ({resp.status_code}): {resp.text[:200]}"
                )
            return True
        if existing.get("scope") != f"/{domain}":
            resp = await client.put(
                f"{_base_url(settings)}/api/users/{existing['id']}",
                headers={"X-Auth": token},
                json={
                    "what": "user",
                    "which": ["scope"],
                    "data": {"id": existing["id"], "username": site_user, "scope": f"/{domain}"},
                },
            )
            if resp.status_code != 200:
                raise FilebrowserError(f"filebrowser scope fix failed ({resp.status_code})")
        return True
    except httpx.TransportError:
        log.warning("filebrowser_unreachable_skipping", site_user=site_user)
        return False
    finally:
        if own:
            await client.aclose()


async def remove_site_user(
    site_user: str,
    settings: Settings,
    *,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Idempotent delete; False when Filebrowser is unreachable or user unknown."""
    validate_site_username(site_user)
    own = client is None
    client = client or _new_client()
    try:
        token = await _admin_token(client, settings)
        existing = next(
            (u for u in await _list_users(client, token, settings) if u["username"] == site_user),
            None,
        )
        if existing is None:
            return False
        resp = await client.request(
            "DELETE",
            f"{_base_url(settings)}/api/users/{existing['id']}",
            headers={"X-Auth": token},
            json={},
        )
        if resp.status_code != 200:
            raise FilebrowserError(f"filebrowser user delete failed ({resp.status_code})")
        return True
    except httpx.TransportError:
        return False
    finally:
        if own:
            await client.aclose()
