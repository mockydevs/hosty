"""Engine and session factory. SQLite runs in WAL mode with foreign keys on."""

from __future__ import annotations

from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def create_engine_and_factory(
    database_url: str,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    kwargs: dict[str, Any] = {}
    in_memory = database_url.endswith(":memory:")
    if in_memory:
        # Tests: one shared in-memory connection across the whole app
        kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}

    engine = create_async_engine(database_url, **kwargs)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            if not in_memory:
                cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine, factory
