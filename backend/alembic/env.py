"""Alembic environment. Respects HOSTY_DATABASE_URL (async URL is converted to sync)."""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from alembic import context
from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("HOSTY_DATABASE_URL", config.get_main_option("sqlalchemy.url"))
    return url.replace("+aiosqlite", "")


def run_migrations_offline() -> None:
    context.configure(url=_database_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
