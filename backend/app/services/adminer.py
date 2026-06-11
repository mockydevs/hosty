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

import structlog

from app.core.config import Settings
from app.core.tickets import issue_token, verify_token

__all__ = ["issue_token", "render_adminer_pool", "verify_token"]

log = structlog.get_logger("hosty.adminer")

TICKET_TTL_SECONDS = 60  # one-time-ish: short window to open the tab
SESSION_COOKIE = "hosty_adminer"

ADMINER_POOL_NAME = "hosty-adminer"
ADMINER_SOCKET = "/run/php/hosty-adminer.sock"


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
