"""Per-site PHP-FPM pool management.

Pool rendering is a pure function (snapshot-tested). Writing/removing pool
files is derived strictly from a validated site user + allowlisted PHP
version, so this layer cannot write outside the PHP pool directories.
"""

from __future__ import annotations

import os

import structlog

from app.core.config import Settings
from app.system import systemd
from app.system.users import validate_site_username

log = structlog.get_logger("hosty.php_fpm")


class InvalidPhpVersionError(ValueError):
    pass


class PoolOperationError(RuntimeError):
    pass


def validate_php_version(version: str, *, allowed: list[str]) -> str:
    if version not in allowed:
        raise InvalidPhpVersionError(
            f"Unsupported PHP version: {version!r} (allowed: {', '.join(allowed)})"
        )
    return version


def fpm_unit(version: str) -> str:
    return f"php{version}-fpm"


def socket_path(site_user: str, version: str, settings: Settings) -> str:
    validate_site_username(site_user)
    return f"{settings.php_socket_dir}/{site_user}-php{version}.sock"


def pool_file_path(site_user: str, version: str, settings: Settings) -> str:
    validate_site_username(site_user)
    validate_php_version(version, allowed=settings.php_versions)
    pool_dir = settings.php_pool_dir_template.format(version=version)
    return f"{pool_dir}/{site_user}.conf"


def render_pool_config(site_user: str, version: str, settings: Settings) -> str:
    """Pure: site user + version in → exact pool config out (snapshot-tested)."""
    validate_site_username(site_user)
    validate_php_version(version, allowed=settings.php_versions)
    socket = socket_path(site_user, version, settings)
    return f"""\
; Managed by Hosty — do not edit by hand.
[{site_user}]
user = {site_user}
group = {site_user}

listen = {socket}
listen.owner = caddy
listen.group = caddy
listen.mode = 0660

pm = ondemand
pm.max_children = 10
pm.process_idle_timeout = 30s
pm.max_requests = 500

php_admin_value[error_log] = /home/{site_user}/php-error.log
php_admin_flag[log_errors] = on
php_admin_value[open_basedir] = none
php_value[memory_limit] = 256M
php_value[upload_max_filesize] = 64M
php_value[post_max_size] = 64M
"""


async def install_pool(site_user: str, version: str, settings: Settings) -> None:
    """Write the pool file and reload PHP-FPM. Idempotent (overwrites)."""
    path = pool_file_path(site_user, version, settings)
    content = render_pool_config(site_user, version, settings)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    log.info("php_pool_installed", pool=path)
    await systemd.control("reload", fpm_unit(version))


async def remove_pool(site_user: str, version: str, settings: Settings) -> bool:
    """Remove the pool file and reload. Idempotent: returns False if absent."""
    path = pool_file_path(site_user, version, settings)
    if not os.path.exists(path):
        return False
    os.unlink(path)
    log.info("php_pool_removed", pool=path)
    await systemd.control("reload", fpm_unit(version))
    return True
