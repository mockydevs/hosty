"""Application factory and ASGI entrypoint."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.routes import auth, databases, files, health, sites, system
from app.core import logging as app_logging
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.core.ratelimit import SlidingWindowLimiter
from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base, create_engine_and_factory


class SPAStaticFiles(StaticFiles):
    """Serve the built frontend; unknown paths fall back to index.html (client routing)."""

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404:
                return await super().get_response("index.html", scope)
            raise


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app_logging.configure(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine, factory = create_engine_and_factory(settings.database_url)
        if settings.create_tables_on_startup:
            # Dev/test convenience; production schemas are managed by Alembic.
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
        app.state.engine = engine
        app.state.sessionmaker = factory
        yield
        await engine.dispose()

    app = FastAPI(
        title="Hosty API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.login_limiter = SlidingWindowLimiter(
        settings.login_rate_limit_attempts, settings.login_rate_limit_window_seconds
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_error_handlers(app)

    app.include_router(health.router, prefix="/api", tags=["health"])
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(system.router, prefix="/api/system", tags=["system"])
    app.include_router(sites.router, prefix="/api/sites", tags=["sites"])
    app.include_router(sites.operations_router, prefix="/api/operations", tags=["operations"])
    app.include_router(databases.router, prefix="/api/databases", tags=["databases"])
    app.include_router(databases.proxy_router)
    app.include_router(files.router, prefix="/api/sites", tags=["files"])
    app.include_router(files.proxy_router)

    dist = Path(settings.frontend_dist)
    if dist.is_dir():  # mounted last so /api always wins
        app.mount("/", SPAStaticFiles(directory=dist, html=True), name="spa")
    return app


app = create_app()
