"""Application configuration. Every value can be overridden with a HOSTY_* env var."""

from __future__ import annotations

from functools import lru_cache
from ipaddress import ip_network
from typing import Annotated, Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="HOSTY_", env_file=".env", extra="ignore")

    env: str = "dev"  # dev | test | prod
    secret_key: str = "dev-only-insecure-secret-change-me"
    database_url: str = "sqlite+aiosqlite:///./hosty.db"
    create_tables_on_startup: bool = True  # prod uses Alembic migrations instead

    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_seconds: int = 14 * 24 * 60 * 60
    cookie_secure: bool = False  # set True in prod (HTTPS only)

    login_rate_limit_attempts: int = 5
    login_rate_limit_window_seconds: int = 60

    cors_origins: list[str] = []
    managed_units: list[str] = ["caddy", "mariadb", "php8.3-fpm", "pdns"]

    # Phase 3: domains & vhosts
    caddy_admin_url: str = "http://127.0.0.1:2019"
    sites_root: str = "/var/www"
    # Dev VMs only: issue site certificates from Caddy's internal CA (no ACME)
    caddy_tls_internal: bool = False
    php_versions: list[str] = ["8.2", "8.3", "8.4"]
    default_php_version: str = "8.3"
    php_pool_dir_template: str = "/etc/php/{version}/fpm/pool.d"
    php_socket_dir: str = "/run/php"

    # Phase 5: databases & Adminer
    adminer_enabled: bool = True
    adminer_root: str = "/var/lib/hosty/adminer"
    adminer_internal_addr: str = "127.0.0.1:8081"
    adminer_session_ttl_seconds: int = 30 * 60

    # Phase 6: file manager (Filebrowser)
    filebrowser_enabled: bool = True
    filebrowser_internal_addr: str = "127.0.0.1:8082"
    filebrowser_db: str = "/var/lib/hosty/filebrowser/filebrowser.db"
    filebrowser_admin_user: str = "admin"  # created at provision time, password locked
    files_session_ttl_seconds: int = 30 * 60

    # Phase 9: hardening
    panel_domain: str | None = None  # production: Caddy fronts the panel on this host
    # Wildcard base for auto-generated stack domains (e.g. "apps.example.com" →
    # "<stack>.apps.example.com"). When unset, generation falls back to sslip.io
    # off `public_ip` ("<stack>.<ip>.sslip.io"), so it works with zero DNS setup.
    apps_base_domain: str | None = None
    panel_upstream: str = "127.0.0.1:8800"
    panel_allowed_ips: Annotated[list[str], NoDecode] = []
    # Where UI-driven config changes (panel domain, cookie flag) are persisted.
    env_file_path: str = "/var/lib/hosty/hosty.env"
    frontend_dist: str = "../frontend/dist"  # served as SPA when the directory exists

    # Phase 7: DNS (PowerDNS)
    dns_enabled: bool = True
    pdns_api_url: str = "http://127.0.0.1:8083/api/v1"  # provision.sh enables this
    pdns_api_key: str = "hosty-dev-key"
    pdns_server_id: str = "localhost"
    # Republish the full Caddy config (sites + panel vhost) at startup.
    caddy_sync_on_startup: bool = True
    dns_default_ttl: int = 3600
    dns_nameservers: list[str] = []  # empty -> ns1.<zone>./ns2.<zone>. (self-hosted)
    public_ip: str = ""  # enables "point to this server" records and templates
    cloudflare_api_token: str = ""  # enables one-click push of zones to Cloudflare

    # Phase 11c/11d: usage metering + uploads
    caddy_access_log_path: str = "/var/log/caddy/hosty-access.log"
    uploads_dir: str = "/var/lib/hosty/uploads"  # staging area for site imports
    max_import_upload_bytes: int = 1024 * 1024 * 1024
    max_user_upload_bytes: int = 2 * 1024 * 1024 * 1024
    max_total_upload_bytes: int = 10 * 1024 * 1024 * 1024
    upload_ttl_seconds: int = 24 * 60 * 60
    max_archive_members: int = 100_000
    max_archive_expanded_bytes: int = 10 * 1024 * 1024 * 1024
    files_proxy_max_request_bytes: int = 1024 * 1024 * 1024
    usage_check_interval_seconds: int = 3600  # disk-quota & health sweep cadence
    disk_full_threshold_percent: int = 90  # server disk "nearly full" notification

    # Phase 12a: containerized apps
    apps_enabled: bool = True
    apps_root: str = "/var/lib/hosty/apps"
    # Loopback ports the panel allocates for container publishing.
    app_port_min: int = 20100
    app_port_max: int = 29999

    # v2 (ADR-013): stack reconciler
    reconcile_enabled: bool = True
    reconcile_interval_seconds: int = 60
    reconcile_concurrency: int = 4  # stacks converged in parallel, host-wide
    reconcile_drift_cycles: int = 3  # consecutive diverged cycles before notifying
    operation_timeout_seconds: int = 600  # hard wall-clock limit per stack operation
    stack_image_auto_update_enabled: bool = True
    stack_image_update_interval_seconds: int = 6 * 3600

    # Phase 8: backups
    backups_root: str = "/var/lib/hosty/backups"
    restore_staging_root: str = "/var/lib/hosty/restore-staging"
    backup_scheduler_enabled: bool = True
    s3_endpoint: str = ""  # e.g. http://127.0.0.1:9000 (MinIO) — empty disables S3
    s3_bucket: str = ""
    s3_region: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_prefix: str = "hosty"

    @field_validator("panel_allowed_ips", mode="before")
    @classmethod
    def parse_panel_allowed_ips(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            value = [item.strip() for item in value.split(",") if item.strip()]
        if not isinstance(value, list):
            raise ValueError("panel_allowed_ips must be a comma-separated list")
        normalized: list[str] = []
        for item in value:
            raw = str(item).strip()
            ip_network(raw, strict=False)
            normalized.append(raw)
        return normalized

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    def validate_for_production(self) -> None:
        """Raise on startup if running in production with insecure defaults."""
        if not self.is_prod:
            return
        _insecure_default = "dev-only-insecure-secret-change-me"
        if self.secret_key == _insecure_default:
            raise RuntimeError(
                "HOSTY_SECRET_KEY is set to the insecure default value. "
                "Set a random 32+ character value via HOSTY_SECRET_KEY before "
                "running in production."
            )
        if self.is_prod and not self.cookie_secure:
            raise RuntimeError(
                "HOSTY_COOKIE_SECURE must be True in production — "
                "set HOSTY_COOKIE_SECURE=true in your environment."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
