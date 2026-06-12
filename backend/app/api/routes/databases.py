"""Databases API (Week 14-15) + the authenticated Adminer proxy.

Credentials appear exactly once in create/reset responses; the panel stores
only a SHA-256 hash (ADR-007).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import fetch_owned_site, get_current_user, get_db, is_admin, require_admin
from app.api.routes.files import PROXY_CSP
from app.core.errors import ConflictError, NotFoundError, UnauthorizedError
from app.core.security import hash_token
from app.db.models import Database, Site, User
from app.services import adminer as adminer_service
from app.services import mariadb, quotas

router = APIRouter(dependencies=[Depends(get_current_user)])


class DatabaseResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    site_id: int
    name: str
    db_user: str
    purpose: str
    created_at: datetime


class DatabaseListEntry(BaseModel):
    database: DatabaseResponse | None = None
    site_domain: str | None = None
    orphan_name: str | None = None  # physical DB with no panel record
    missing: bool = False  # panel record whose physical DB is gone


class CreateDatabaseRequest(BaseModel):
    name: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]{1,63}$")


class CredentialsResponse(BaseModel):
    database: DatabaseResponse
    password: str  # shown exactly once — never retrievable again


class DeleteDatabaseRequest(BaseModel):
    confirm_name: str


class AdminerSessionResponse(BaseModel):
    url: str


@router.get("", response_model=list[DatabaseListEntry])
async def list_databases(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> Any:
    query = (
        select(Database, Site.domain)
        .join(Site, Site.id == Database.site_id)
        .order_by(Database.name)
    )
    if not is_admin(user):
        query = query.where(Site.owner_id == user.id)
    rows = (await db.execute(query)).all()

    physical: set[str] | None = None
    if is_admin(user):  # orphan/missing detection is server-wide -> admin only
        try:
            physical = set(await mariadb.list_physical_databases())
        except mariadb.MariaDBError:
            physical = None  # server unreachable: skip orphan detection

    entries = [
        DatabaseListEntry(
            database=DatabaseResponse.model_validate(database),
            site_domain=domain,
            missing=physical is not None and database.name not in physical,
        )
        for database, domain in rows
    ]
    if physical is not None:
        known = {database.name for database, _ in rows}
        entries.extend(DatabaseListEntry(orphan_name=name) for name in sorted(physical - known))
    return entries


@router.post(
    "/sites/{site_id}", response_model=CredentialsResponse, status_code=status.HTTP_201_CREATED
)
async def create_database(
    site_id: int,
    body: CreateDatabaseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    site = await fetch_owned_site(db, user, site_id)
    if site.status != "active":
        raise ConflictError(f"Site is {site.status}; wait until it is active")

    limits = await quotas.effective_limits(db, user)
    if not is_admin(user) and limits.max_databases is not None:
        owned = (
            await db.execute(
                select(func.count())
                .select_from(Database)
                .join(Site, Site.id == Database.site_id)
                .where(Site.owner_id == user.id)
            )
        ).scalar_one()
        if owned >= limits.max_databases:
            raise ConflictError(f"Database quota reached ({limits.max_databases})")

    name = mariadb.validate_identifier(body.name)
    duplicate = (
        await db.execute(
            select(Database).where((Database.name == name) | (Database.db_user == name))
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        raise ConflictError(f"A database named {name} already exists")

    password = mariadb.generate_password()
    await mariadb.create_database(name, name, password)
    row = Database(
        site_id=site.id,
        name=name,
        db_user=name,
        purpose="custom",
        password_hash=hash_token(password),
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return CredentialsResponse(database=DatabaseResponse.model_validate(row), password=password)


async def _fetch_owned_database(db: AsyncSession, user: User, database_id: int) -> Database:
    """404 (not 403) for other tenants' databases — existence never leaks."""
    row = await db.get(Database, database_id)
    if row is None:
        raise NotFoundError("Database not found")
    if not is_admin(user):
        site = await db.get(Site, row.site_id)
        if site is None or site.owner_id != user.id:
            raise NotFoundError("Database not found")
    return row


@router.delete("/{database_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_database(
    database_id: int,
    body: DeleteDatabaseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    row = await _fetch_owned_database(db, user, database_id)
    if body.confirm_name.strip() != row.name:
        raise ConflictError("Confirmation does not match the database name")
    await mariadb.drop_database(row.name, row.db_user)
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(
    "/orphans/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
)
async def delete_orphan_database(
    name: str, body: DeleteDatabaseRequest, db: AsyncSession = Depends(get_db)
) -> Response:
    """Drop a physical database that has no panel record (shown as 'orphan').

    Guards: never a system schema, never a panel-managed database (those go
    through their own delete, which also removes the DB user), must actually
    exist, and the name must be typed back to confirm.
    """
    try:
        mariadb.validate_identifier(name)
    except mariadb.InvalidIdentifierError as exc:
        raise ConflictError(str(exc)) from exc
    if name in mariadb.SYSTEM_SCHEMAS:
        raise ConflictError("System schemas cannot be deleted")

    managed = (await db.execute(select(Database).where(Database.name == name))).scalar_one_or_none()
    if managed is not None:
        raise ConflictError(
            "This database is managed by the panel — delete it from its own entry instead"
        )
    if name not in set(await mariadb.list_physical_databases()):
        raise NotFoundError(f"No database named {name} on the server")
    if body.confirm_name.strip() != name:
        raise ConflictError("Confirmation does not match the database name")

    await mariadb.drop_orphan_database(name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{database_id}/reset-password", response_model=CredentialsResponse)
async def reset_password(
    database_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    row = await _fetch_owned_database(db, user, database_id)
    password = mariadb.generate_password()
    await mariadb.reset_password(row.db_user, password)
    row.password_hash = hash_token(password)
    await db.commit()
    await db.refresh(row)
    return CredentialsResponse(database=DatabaseResponse.model_validate(row), password=password)


@router.post(
    "/adminer-session",
    response_model=AdminerSessionResponse,
    dependencies=[Depends(require_admin)],
)
async def adminer_session(request: Request) -> Any:
    """Mint a short-lived ticket; the /adminer proxy swaps it for a cookie.

    Admin-only: Adminer's login form takes any server credentials, so the
    proxy must not be reachable by client tenants (Phase 11a)."""
    settings = request.app.state.settings
    ticket = adminer_service.issue_token(
        secret=settings.secret_key,
        ttl_seconds=adminer_service.TICKET_TTL_SECONDS,
        scope="ticket",
    )
    return AdminerSessionResponse(url=f"/adminer/?hosty_ticket={ticket}")


# --- Adminer reverse proxy (cookie/ticket auth, NOT bearer auth) -------------------

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


@proxy_router.api_route("/adminer{path:path}", methods=["GET", "POST"], include_in_schema=False)
async def adminer_proxy(request: Request, path: str) -> Response:
    settings = request.app.state.settings
    if not settings.adminer_enabled:
        raise NotFoundError("Adminer is disabled")

    authorized = False
    set_session_cookie = False
    ticket = request.query_params.get("hosty_ticket")
    if ticket and adminer_service.verify_token(ticket, secret=settings.secret_key, scope="ticket"):
        authorized = True
        set_session_cookie = True
    else:
        cookie = request.cookies.get(adminer_service.SESSION_COOKIE)
        if cookie and adminer_service.verify_token(
            cookie, secret=settings.secret_key, scope="session"
        ):
            authorized = True
    if not authorized:
        raise UnauthorizedError("Adminer session expired — reopen it from the panel")

    upstream = f"http://{settings.adminer_internal_addr}{path or '/'}"
    params = [(k, v) for k, v in request.query_params.multi_items() if k != "hosty_ticket"]
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in {"host", "authorization", "cookie", *HOP_BY_HOP}
    }
    body = await request.body()

    client: httpx.AsyncClient | None = getattr(request.app.state, "adminer_http_client", None)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        upstream_resp = await client.request(
            request.method, upstream, params=params, headers=headers, content=body
        )
    except httpx.HTTPError as exc:
        raise NotFoundError(f"Adminer backend unreachable: {exc.__class__.__name__}") from exc
    finally:
        if own_client:
            await client.aclose()

    response_headers = {
        k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP
    }
    # Adminer's UI relies on inline scripts/styles, which the strict panel-wide
    # CSP would block (blank page) — see PROXY_CSP in routes/files.py.
    response_headers["Content-Security-Policy"] = PROXY_CSP
    response = Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers=response_headers,
    )
    if set_session_cookie:
        response.set_cookie(
            adminer_service.SESSION_COOKIE,
            adminer_service.issue_token(
                secret=settings.secret_key,
                ttl_seconds=settings.adminer_session_ttl_seconds,
                scope="session",
            ),
            max_age=settings.adminer_session_ttl_seconds,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            path="/adminer",
        )
    return response
