"""Application factory and ASGI entrypoint."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.routes import (
    apps,
    audit,
    auth,
    backups,
    databases,
    dns,
    files,
    health,
    notifications,
    plans,
    sites,
    stacks,
    system,
    usage,
    users,
)
from app.core import logging as app_logging
from app.core.config import Settings, get_settings
from app.core.errors import register_error_handlers
from app.core.middleware import (
    AuditLogMiddleware,
    IPAllowlistMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)
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
        if settings.caddy_sync_on_startup:
            # Publish the FULL desired state (sites + panel vhost) on every
            # startup: Caddy restarts/reboots boot from the stock Caddyfile and
            # lose everything applied via the admin API — without this sync,
            # all sites stay down until the next site mutation.
            # Best-effort — Caddy may not be up yet; the next sync repairs it.
            try:
                from app.services import sites as sites_service
                from app.services.caddy import CaddyClient

                async with factory() as db:
                    await CaddyClient(settings.caddy_admin_url).apply(
                        await sites_service.build_full_config(db, settings)
                    )
            except Exception as exc:
                structlog.get_logger("hosty.startup").warning(
                    "initial_caddy_sync_failed", error=str(exc)
                )

        if settings.adminer_enabled:
            try:
                from pathlib import Path

                from app.services.adminer import ADMINER_POOL_NAME, render_adminer_pool
                from app.system import systemd

                pool_dir = Path(
                    settings.php_pool_dir_template.format(version=settings.default_php_version)
                )
                pool_path = pool_dir / f"{ADMINER_POOL_NAME}.conf"
                pool_content = render_adminer_pool(settings)

                if not pool_path.exists() or pool_path.read_text(encoding="utf-8") != pool_content:
                    pool_dir.mkdir(parents=True, exist_ok=True)
                    pool_path.write_text(pool_content, encoding="utf-8")
                    await systemd.control("reload", f"php{settings.default_php_version}-fpm")
            except Exception as exc:
                structlog.get_logger("hosty.startup").warning(
                    "adminer_pool_setup_failed", error=str(exc)
                )
        scheduler_task: asyncio.Task | None = None
        if settings.backup_scheduler_enabled and settings.env != "test":
            from app.services import scheduler

            scheduler_task = asyncio.create_task(scheduler.loop(app))

        # v2 (ADR-013): the stack reconciler — converge on startup (heals
        # drift from downtime/reboots), then the interval loop. Fully wired
        # (M4): DB-backed desired state, full ingress sync, status
        # projection onto the stacks table.
        from app.services import stacks as stacks_service

        reconciler = stacks_service.build_reconciler(factory, settings)
        app.state.reconciler = reconciler
        reconciler_task: asyncio.Task | None = None
        if settings.reconcile_enabled and settings.env != "test":
            try:
                await reconciler.converge_all()
            except Exception as exc:
                structlog.get_logger("hosty.startup").warning(
                    "startup_converge_failed", error=str(exc)
                )
            reconciler_task = asyncio.create_task(reconciler.run_loop())
        yield
        for task in (scheduler_task, reconciler_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await engine.dispose()

    app = FastAPI(
        title="HostyPanel API",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.login_limiter = SlidingWindowLimiter(
        settings.login_rate_limit_attempts, settings.login_rate_limit_window_seconds
    )
    app.state.upload_lock = asyncio.Lock()

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(AuditLogMiddleware)
    app.add_middleware(IPAllowlistMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_error_handlers(app)

    app.include_router(health.router, prefix="/api", tags=["health"])
    app.include_router(audit.router, prefix="/api/audit", tags=["audit"])
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(users.router, prefix="/api/users", tags=["users"])
    app.include_router(system.router, prefix="/api/system", tags=["system"])
    app.include_router(sites.router, prefix="/api/sites", tags=["sites"])
    app.include_router(apps.router, prefix="/api/apps", tags=["apps"])
    app.include_router(stacks.router, prefix="/api/stacks", tags=["stacks"])
    app.include_router(sites.operations_router, prefix="/api/operations", tags=["operations"])
    app.include_router(databases.router, prefix="/api/databases", tags=["databases"])
    app.include_router(databases.proxy_router)
    app.include_router(files.router, prefix="/api/sites", tags=["files"])
    app.include_router(files.proxy_router)
    app.include_router(dns.router, prefix="/api/dns", tags=["dns"])
    app.include_router(plans.router, prefix="/api/plans", tags=["plans"])
    app.include_router(notifications.router, prefix="/api/notifications", tags=["notifications"])
    app.include_router(usage.router, prefix="/api/usage", tags=["usage"])
    app.include_router(backups.router, prefix="/api/backups", tags=["backups"])
    app.include_router(backups.site_router, prefix="/api/sites", tags=["backups"])

    dist = Path(settings.frontend_dist)
    if dist.is_dir():  # mounted last so /api always wins
        app.mount("/", SPAStaticFiles(directory=dist, html=True), name="spa")
    return app


app = create_app()
