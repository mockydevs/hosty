"""Per-site PHP-FPM pool management.

Pool rendering is a pure function (snapshot-tested). Writing/removing pool
files is derived strictly from a validated site user + allowlisted PHP
version, so this layer cannot write outside the PHP pool directories.
"""

from __future__ import annotations

import os
import re

import structlog

from app.core.config import Settings
from app.system import systemd
from app.system.users import validate_site_username

log = structlog.get_logger("hosty.php_fpm")

# 1-4 digits + M (megabytes). Keeps values shell- and ini-safe by construction.
PHP_SIZE_RE = re.compile(r"^[1-9][0-9]{0,3}M$")


class InvalidPhpVersionError(ValueError):
    pass


class InvalidPhpSettingError(ValueError):
    pass


class PoolOperationError(RuntimeError):
    pass


def validate_php_version(version: str, *, allowed: list[str]) -> str:
    if version not in allowed:
        raise InvalidPhpVersionError(
            f"Unsupported PHP version: {version!r} (allowed: {', '.join(allowed)})"
        )
    return version


def validate_php_size(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not PHP_SIZE_RE.fullmatch(value):
        raise InvalidPhpSettingError(f"{name} must look like '256M', got {value!r}")
    return value


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


def render_pool_config(
    site_user: str,
    version: str,
    settings: Settings,
    *,
    memory_limit: str = "256M",
    upload_max_filesize: str = "64M",
) -> str:
    """Pure: site user + version + limits in → exact pool file out."""
    validate_site_username(site_user)
    validate_php_version(version, allowed=settings.php_versions)
    validate_php_size(memory_limit, name="memory_limit")
    validate_php_size(upload_max_filesize, name="upload_max_filesize")
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
php_value[memory_limit] = {memory_limit}
php_value[upload_max_filesize] = {upload_max_filesize}
php_value[post_max_size] = {upload_max_filesize}
"""


async def install_pool(
    site_user: str,
    version: str,
    settings: Settings,
    *,
    memory_limit: str = "256M",
    upload_max_filesize: str = "64M",
) -> None:
    """Write the pool file and reload PHP-FPM. Idempotent (overwrites)."""
    path = pool_file_path(site_user, version, settings)
    content = render_pool_config(
        site_user,
        version,
        settings,
        memory_limit=memory_limit,
        upload_max_filesize=upload_max_filesize,
    )
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
