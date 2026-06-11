"""Application configuration. Every value can be overridden with a HOSTY_* env var."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    return Settings()
