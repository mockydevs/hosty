"""File manager (Week 16): site-bound sessions + the authenticated proxy.

The proxy injects the Filebrowser auth header from the SIGNED session scope —
the client can never choose whose files it sees (spoofed headers stripped).
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.errors import ConflictError, NotFoundError, UnauthorizedError
from app.core.tickets import issue_token, token_scope
from app.db.models import Site
from app.services.filebrowser import AUTH_HEADER

router = APIRouter(dependencies=[Depends(get_current_user)])

SESSION_COOKIE = "hosty_files"
TICKET_TTL_SECONDS = 60

# Scopes carry the site user: "files-ticket:<site_user>" / "files:<site_user>".
TICKET_PREFIX = "files-ticket:"
SESSION_PREFIX = "files:"


class FilesSessionResponse(BaseModel):
    url: str


@router.post("/{site_id}/files-session", response_model=FilesSessionResponse)
async def files_session(
    request: Request, site_id: int, db: AsyncSession = Depends(get_db)
) -> FilesSessionResponse:
    settings = request.app.state.settings
    if not settings.filebrowser_enabled:
        raise NotFoundError("File manager is disabled")
    site = await db.get(Site, site_id)
    if site is None:
        raise NotFoundError("Site not found")
    if site.status != "active":
        raise ConflictError(f"Site is {site.status}; wait until it is active")
    ticket = issue_token(
        secret=settings.secret_key,
        ttl_seconds=TICKET_TTL_SECONDS,
        scope=f"{TICKET_PREFIX}{site.site_user}",
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


def _session_site_user(request: Request) -> tuple[str, bool]:
    """(site_user, needs_cookie) from a valid ticket or session cookie."""
    settings = request.app.state.settings
    ticket = request.query_params.get("hosty_ticket")
    if ticket:
        scope = token_scope(ticket, secret=settings.secret_key)
        if scope and scope.startswith(TICKET_PREFIX):
            return scope.removeprefix(TICKET_PREFIX), True
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        scope = token_scope(cookie, secret=settings.secret_key)
        if scope and scope.startswith(SESSION_PREFIX):
            return scope.removeprefix(SESSION_PREFIX), False
    raise UnauthorizedError("File manager session expired — reopen it from the panel")


@proxy_router.api_route(
    "/files{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    include_in_schema=False,
)
async def files_proxy(request: Request, path: str) -> Response:
    settings = request.app.state.settings
    if not settings.filebrowser_enabled:
        raise NotFoundError("File manager is disabled")

    site_user, set_cookie = _session_site_user(request)

    # Filebrowser runs with baseurl=/files (assets resolve under the proxy
    # path), and it strips that prefix itself — forward the full path.
    upstream = f"http://{settings.filebrowser_internal_addr}/files{path or '/'}"
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "hosty_ticket"]
    headers = {
        k: v
        for k, v in request.headers.items()
        # Strip anything that could spoof identity; the panel sets it below.
        if k.lower() not in {"host", "authorization", "cookie", AUTH_HEADER.lower(), *HOP_BY_HOP}
    }
    headers[AUTH_HEADER] = site_user
    body = await request.body()

    client: httpx.AsyncClient | None = getattr(request.app.state, "files_http_client", None)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=60.0)
    try:
        upstream_resp = await client.request(
            request.method, upstream, params=params, headers=headers, content=body
        )
    except httpx.HTTPError as exc:
        raise NotFoundError(f"File manager unreachable: {exc.__class__.__name__}") from exc
    finally:
        if own_client:
            await client.aclose()

    response = Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers={k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP},
    )
    if set_cookie:
        response.set_cookie(
            SESSION_COOKIE,
            issue_token(
                secret=settings.secret_key,
                ttl_seconds=settings.files_session_ttl_seconds,
                scope=f"{SESSION_PREFIX}{site_user}",
            ),
            max_age=settings.files_session_ttl_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/files",
        )
    return response
