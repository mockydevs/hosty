"""PHP-FPM pool: version allowlist + exact pool config snapshot."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.php_fpm import (
    InvalidPhpVersionError,
    fpm_unit,
    pool_file_path,
    render_pool_config,
    socket_path,
    validate_php_version,
)


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def test_validate_php_version(settings):
    assert validate_php_version("8.3", allowed=settings.php_versions) == "8.3"
    for bad in ["7.4", "8.3; rm -rf /", "../8.3", ""]:
        with pytest.raises(InvalidPhpVersionError):
            validate_php_version(bad, allowed=settings.php_versions)


def test_paths(settings):
    assert fpm_unit("8.3") == "php8.3-fpm"
    assert socket_path("site-a-abc123", "8.3", settings) == "/run/php/site-a-abc123-php8.3.sock"
    assert (
        pool_file_path("site-a-abc123", "8.3", settings)
        == "/etc/php/8.3/fpm/pool.d/site-a-abc123.conf"
    )


def test_pool_path_rejects_bad_user(settings):
    from app.system.users import InvalidSiteUserError

    with pytest.raises(InvalidSiteUserError):
        pool_file_path("../../etc/cron.d/evil", "8.3", settings)


def test_render_pool_config_snapshot(settings):
    """Site user + version in → exact pool file out."""
    expected = """\
; Managed by HostyPanel — do not edit by hand.
[site-a-abc123]
user = site-a-abc123
group = site-a-abc123

listen = /run/php/site-a-abc123-php8.3.sock
listen.owner = caddy
listen.group = caddy
listen.mode = 0660

pm = ondemand
pm.max_children = 10
pm.process_idle_timeout = 30s
pm.max_requests = 500

php_admin_value[error_log] = /home/site-a-abc123/php-error.log
php_admin_flag[log_errors] = on
php_admin_value[open_basedir] = /var/www/a.example/public_html:/home/site-a-abc123:/tmp
php_value[memory_limit] = 256M
php_value[upload_max_filesize] = 64M
php_value[post_max_size] = 64M
"""
    assert (
        render_pool_config(
            "site-a-abc123",
            "8.3",
            settings,
            doc_root="/var/www/a.example/public_html",
        )
        == expected
    )
