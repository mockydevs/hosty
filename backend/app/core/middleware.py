"""HTTP middleware: security headers and request context/access logging."""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.background import BackgroundTask, BackgroundTasks
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

access_log = structlog.get_logger("hosty.access")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'")
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=63072000; includeSubDomains"
            )
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=request_id)
        start = time.perf_counter()
        try:
            response = await call_next(request)
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            access_log.info(
                "request",
                method=request.method,
                path=request.url.path,
                status=response.status_code,
                duration_ms=duration_ms,
            )
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            structlog.contextvars.unbind_contextvars("request_id")


MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
AUDIT_EXCLUDED_PATHS = frozenset({"/api/auth/refresh"})  # rotation noise, not an action


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Record every mutating API request (Week 22).

    Bodies are never read or stored — they can contain passwords. Failures to
    write the audit row never break the request; they are logged instead.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        path = request.url.path
        if (
            request.method not in MUTATING_METHODS
            or not path.startswith("/api/")
            or path in AUDIT_EXCLUDED_PATHS
        ):
            return response
        # Run AFTER the response (and any route background work) completes:
        # never adds latency and never interleaves with the request's session.
        audit_task = BackgroundTask(self._safe_record, request, path, response.status_code)
        existing = response.background
        if existing is None:
            response.background = audit_task
        else:
            chained = BackgroundTasks()
            if isinstance(existing, BackgroundTasks):
                chained.tasks.extend(existing.tasks)
            else:
                chained.tasks.append(existing)
            chained.tasks.append(audit_task)
            response.background = chained
        return response

    async def _safe_record(self, request: Request, path: str, status_code: int) -> None:
        try:
            await self._record(request, path, status_code)
        except Exception:  # pragma: no cover - audit must never break requests
            structlog.get_logger("hosty.audit").exception("audit_write_failed", path=path)

    async def _record(self, request: Request, path: str, status_code: int) -> None:
        from app.core.security import decode_access_token
        from app.db.models import AuditLog, User

        settings = request.app.state.settings
        sessionmaker = getattr(request.app.state, "sessionmaker", None)
        if sessionmaker is None:  # lifespan not started (unit tests of bare app)
            return

        user_id: int | None = None
        username: str | None = None
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            try:
                payload = decode_access_token(auth[7:], secret=settings.secret_key)
                user_id = int(payload["sub"])
            except Exception:  # invalid/expired token: record as anonymous
                user_id = None

        async with sessionmaker() as db:
            if user_id is not None:
                user = await db.get(User, user_id)
                username = user.username if user else None
            db.add(
                AuditLog(
                    user_id=user_id,
                    username=username,
                    method=request.method,
                    path=path[:255],
                    status_code=status_code,
                    client_ip=request.client.host if request.client else None,
                )
            )
            await db.commit()


class IPAllowlistMiddleware(BaseHTTPMiddleware):
    """Optional defense-in-depth: reject clients outside `panel_allowed_ips`.

    Empty allowlist (the default) allows everyone. Caddy enforces the same
    list at the edge in production; this guards direct-to-uvicorn access.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        allowed = request.app.state.settings.panel_allowed_ips
        if allowed:
            client_ip = request.client.host if request.client else None
            if client_ip not in allowed:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "ip_not_allowed",
                            "message": "This IP address is not allowed to access the panel",
                            "details": None,
                        }
                    },
                )
        return await call_next(request)
