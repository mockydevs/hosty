"""File manager (Week 16): site-bound sessions + the authenticated proxy.

The proxy injects the Filebrowser auth header from the SIGNED session scope —
the client can never choose whose files it sees (spoofed headers stripped).
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.api.deps import fetch_owned_site, get_current_user, get_db, is_admin
from app.core.errors import ConflictError, NotFoundError, UnauthorizedError
from app.core.tickets import issue_token, token_scope
from app.db.models import Site, Stack, StackService, User
from app.services.filebrowser import AUTH_HEADER

WP_FILES_SERVICE = "files"

router = APIRouter(dependencies=[Depends(get_current_user)])
stack_router = APIRouter(dependencies=[Depends(get_current_user)])

SESSION_COOKIE = "hosty_files"
TICKET_TTL_SECONDS = 60

# Scopes bind the site, actor, and actor security version. The proxy rechecks
# current account and ownership state on every request.
TICKET_PREFIX = "files-ticket:"
SESSION_PREFIX = "files:"
STACK_TICKET_PREFIX = "files-stack-ticket:"
STACK_SESSION_PREFIX = "files-stack:"


class FilesSessionResponse(BaseModel):
    url: str


@router.post("/{site_id}/files-session", response_model=FilesSessionResponse)
async def files_session(
    request: Request,
    site_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FilesSessionResponse:
    settings = request.app.state.settings
    if not settings.filebrowser_enabled:
        raise NotFoundError("File manager is disabled")
    site = await fetch_owned_site(db, user, site_id)
    if site.status != "active":
        raise ConflictError(f"Site is {site.status}; wait until it is active")
    ticket = issue_token(
        secret=settings.secret_key,
        ttl_seconds=TICKET_TTL_SECONDS,
        scope=f"{TICKET_PREFIX}{site.site_user}:{user.id}:{user.token_version}",
    )
    return FilesSessionResponse(url=f"/files/?hosty_ticket={ticket}")


async def _fetch_owned_stack(db: AsyncSession, user: User, stack_id: int) -> Stack:
    stack = await db.get(Stack, stack_id)
    if stack is None or (not is_admin(user) and stack.owner_id != user.id):
        raise NotFoundError("Stack not found")
    return stack


async def _stack_files_service(db: AsyncSession, stack_id: int) -> StackService:
    row = (
        await db.execute(
            select(StackService).where(
                StackService.stack_id == stack_id, StackService.name == WP_FILES_SERVICE
            )
        )
    ).scalar_one_or_none()
    if row is None or row.internal_port is None:
        raise NotFoundError("Stack has no file manager")
    return row


@stack_router.post("/{stack_id}/files-session", response_model=FilesSessionResponse)
async def stack_files_session(
    request: Request,
    stack_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> FilesSessionResponse:
    settings = request.app.state.settings
    if not settings.filebrowser_enabled:
        raise NotFoundError("File manager is disabled")
    stack = await _fetch_owned_stack(db, user, stack_id)
    if stack.status != "ready":
        raise ConflictError(f"Stack is {stack.status}; wait until it is ready")
    await _stack_files_service(db, stack.id)
    ticket = issue_token(
        secret=settings.secret_key,
        ttl_seconds=TICKET_TTL_SECONDS,
        scope=f"{STACK_TICKET_PREFIX}{stack.id}:{user.id}:{user.token_version}",
    )
    return FilesSessionResponse(url=f"/files/?hosty_ticket={ticket}")


proxy_router = APIRouter()

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "content-encoding",
}

# Proxied apps (Filebrowser here, Adminer in routes/databases.py) bootstrap
# from inline <script>/<style> tags in their HTML. The panel-wide CSP
# (default-src 'self', no unsafe-inline) blocks those, leaving a blank page.
# This relaxed policy is set ONLY on the proxied responses — the panel itself
# keeps the strict default (the middleware uses setdefault).
PROXY_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'self'"
)


async def _limited_body(request: Request, limit: int):
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ConflictError("File-manager request exceeds the configured size limit")
        yield chunk


async def _session_target(
    request: Request, db: AsyncSession
) -> tuple[str, str | None, str, bool, User]:
    """Resolve and authorize a ticket/cookie against current database state."""
    settings = request.app.state.settings
    ticket = request.query_params.get("hosty_ticket")
    if ticket:
        scope = token_scope(ticket, secret=settings.secret_key)
        needs_cookie = True
    else:
        cookie = request.cookies.get(SESSION_COOKIE)
        scope = token_scope(cookie, secret=settings.secret_key) if cookie else None
        needs_cookie = False
    if not scope:
        raise UnauthorizedError("File manager session expired — reopen it from the panel")

    if scope.startswith(STACK_TICKET_PREFIX) or scope.startswith(STACK_SESSION_PREFIX):
        prefix = (
            STACK_TICKET_PREFIX if scope.startswith(STACK_TICKET_PREFIX) else STACK_SESSION_PREFIX
        )
        try:
            stack_id_raw, user_id_raw, version_raw = scope.removeprefix(prefix).rsplit(":", 2)
            stack_id = int(stack_id_raw)
            user_id = int(user_id_raw)
            token_version = int(version_raw)
        except (TypeError, ValueError):
            raise UnauthorizedError(
                "File manager session expired — reopen it from the panel"
            ) from None

        user = await db.get(User, user_id)
        stack = await db.get(Stack, stack_id)
        if (
            user is None
            or user.suspended
            or user.token_version != token_version
            or stack is None
            or stack.status != "ready"
            or (user.role != "admin" and stack.owner_id != user.id)
        ):
            raise UnauthorizedError("File manager session is no longer authorized")
        svc = await _stack_files_service(db, stack.id)
        session_scope = f"{STACK_SESSION_PREFIX}{stack.id}:{user.id}:{user.token_version}"
        return f"{stack.loopback_ip}:{svc.internal_port}", None, session_scope, needs_cookie, user

    prefix = TICKET_PREFIX if scope.startswith(TICKET_PREFIX) else SESSION_PREFIX
    if not scope.startswith(prefix):
        raise UnauthorizedError("File manager session expired — reopen it from the panel")
    try:
        site_user, user_id_raw, version_raw = scope.removeprefix(prefix).rsplit(":", 2)
        user_id = int(user_id_raw)
        token_version = int(version_raw)
    except (TypeError, ValueError):
        raise UnauthorizedError("File manager session expired — reopen it from the panel") from None

    user = await db.get(User, user_id)
    site = (await db.execute(select(Site).where(Site.site_user == site_user))).scalar_one_or_none()
    if (
        user is None
        or user.suspended
        or user.token_version != token_version
        or site is None
        or site.status != "active"
        or (user.role != "admin" and site.owner_id != user.id)
    ):
        raise UnauthorizedError("File manager session is no longer authorized")
    session_scope = f"{SESSION_PREFIX}{site_user}:{user.id}:{user.token_version}"
    return settings.filebrowser_internal_addr, site_user, session_scope, needs_cookie, user


@proxy_router.api_route(
    "/files{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def files_proxy(request: Request, path: str, db: AsyncSession = Depends(get_db)) -> Response:
    settings = request.app.state.settings
    if not settings.filebrowser_enabled:
        raise NotFoundError("File manager is disabled")

    upstream_addr, filebrowser_user, session_scope, set_cookie, _actor = await _session_target(
        request, db
    )

    # Filebrowser runs with baseurl=/files (assets resolve under the proxy
    # path), and it strips that prefix itself — forward the full path.
    upstream = f"http://{upstream_addr}/files{path or '/'}"
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "hosty_ticket"]
    headers = {
        k: v
        for k, v in request.headers.items()
        # Strip anything that could spoof identity; the panel sets it below.
        if k.lower() not in {"host", "authorization", "cookie", AUTH_HEADER.lower(), *HOP_BY_HOP}
    }
    if filebrowser_user is not None:
        headers[AUTH_HEADER] = filebrowser_user
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_size = int(content_length)
        except ValueError:
            raise ConflictError("Invalid Content-Length header") from None
        if declared_size < 0:
            raise ConflictError("Invalid Content-Length header")
        if declared_size > settings.files_proxy_max_request_bytes:
            raise ConflictError("File-manager request exceeds the configured size limit")

    client: httpx.AsyncClient | None = getattr(request.app.state, "files_http_client", None)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=60.0)
    try:
        upstream_request = client.build_request(
            request.method,
            upstream,
            params=params,
            headers=headers,
            content=_limited_body(request, settings.files_proxy_max_request_bytes),
        )
        upstream_resp = await client.send(upstream_request, stream=True)
    except Exception as exc:
        if own_client:
            await client.aclose()
        if isinstance(exc, httpx.HTTPError):
            raise NotFoundError(f"File manager unreachable: {exc.__class__.__name__}") from exc
        raise

    async def close_upstream() -> None:
        await upstream_resp.aclose()
        if own_client:
            await client.aclose()

    response_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP
    }
    # The panel embeds the file manager in an iframe on the site detail page;
    # SAMEORIGIN here pre-empts the global X-Frame-Options: DENY (setdefault),
    # and PROXY_CSP pre-empts the strict global CSP that would otherwise block
    # Filebrowser's inline bootstrap script (blank iframe).
    response_headers["X-Frame-Options"] = "SAMEORIGIN"
    response_headers["Content-Security-Policy"] = PROXY_CSP

    # Mock transports and response hooks may buffer the body even when send()
    # was asked to stream it. Real network responses retain the streaming path.
    async def response_body():
        try:
            if upstream_resp.is_stream_consumed:
                yield upstream_resp.content
            else:
                async for chunk in upstream_resp.aiter_raw():
                    yield chunk
        finally:
            await close_upstream()

    response = StreamingResponse(
        response_body(),
        status_code=upstream_resp.status_code,
        headers=response_headers,
    )
    if set_cookie:
        response.set_cookie(
            SESSION_COOKIE,
            issue_token(
                secret=settings.secret_key,
                ttl_seconds=settings.files_session_ttl_seconds,
                scope=session_scope,
            ),
            max_age=settings.files_session_ttl_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/files",
        )
    return response
