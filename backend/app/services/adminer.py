"""Adminer behind panel auth (ADR-007).

Adminer (a single PHP file) is served by Caddy on an INTERNAL-ONLY listener
and reached exclusively through the panel's authenticated reverse proxy at
/adminer. Access uses short-lived HMAC tickets minted for a logged-in admin;
the proxy exchanges the ticket for a signed session cookie.

Credential auto-fill into Adminer's login form is deliberately NOT done in v1
(it would require shipping an Adminer login plugin); the user pastes the
password from the show-once dialog. Tradeoff documented in ADR-007.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import structlog

from app.core.config import Settings

log = structlog.get_logger("hosty.adminer")

TICKET_TTL_SECONDS = 60  # one-time-ish: short window to open the tab
SESSION_COOKIE = "hosty_adminer"

ADMINER_POOL_NAME = "hosty-adminer"
ADMINER_SOCKET = "/run/php/hosty-adminer.sock"


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_token(*, secret: str, ttl_seconds: int, scope: str = "session") -> str:
    """`<scope>.<expiry>.<hmac>` — verifiable statelessly, expires on its own."""
    expiry = str(int(time.time()) + ttl_seconds)
    payload = f"{scope}.{expiry}"
    return f"{payload}.{_sign(payload, secret)}"


def verify_token(token: str, *, secret: str, scope: str = "session") -> bool:
    parts = token.split(".")
    if len(parts) != 3:
        return False
    token_scope, expiry, signature = parts
    if token_scope != scope or not expiry.isdigit():
        return False
    payload = f"{token_scope}.{expiry}"
    if not hmac.compare_digest(signature, _sign(payload, secret)):
        return False
    return int(expiry) >= time.time()


def render_adminer_pool(settings: Settings) -> str:
    """Pure: dedicated low-privilege pool for Adminer (snapshot-tested)."""
    return f"""\
; Managed by Hosty — do not edit by hand.
[{ADMINER_POOL_NAME}]
user = www-data
group = www-data

listen = {ADMINER_SOCKET}
listen.owner = caddy
listen.group = caddy
listen.mode = 0660

pm = ondemand
pm.max_children = 4
pm.process_idle_timeout = 30s

php_admin_value[open_basedir] = {settings.adminer_root}:/tmp
php_admin_flag[log_errors] = on
php_value[memory_limit] = 128M
php_value[upload_max_filesize] = 64M
php_value[post_max_size] = 64M
"""
