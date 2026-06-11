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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.errors import ConflictError, NotFoundError, UnauthorizedError
from app.core.security import hash_token
from app.db.models import Database, Site
from app.services import adminer as adminer_service
from app.services import mariadb

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
async def list_databases(db: AsyncSession = Depends(get_db)) -> Any:
    rows = (
        await db.execute(
            select(Database, Site.domain)
            .join(Site, Site.id == Database.site_id)
            .order_by(Database.name)
        )
    ).all()
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
    site_id: int, body: CreateDatabaseRequest, db: AsyncSession = Depends(get_db)
) -> Any:
    site = await db.get(Site, site_id)
    if site is None:
        raise NotFoundError("Site not found")
    if site.status != "active":
        raise ConflictError(f"Site is {site.status}; wait until it is active")

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


@router.delete("/{database_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_database(
    database_id: int, body: DeleteDatabaseRequest, db: AsyncSession = Depends(get_db)
) -> Response:
    row = await db.get(Database, database_id)
    if row is None:
        raise NotFoundError("Database not found")
    if body.confirm_name.strip() != row.name:
        raise ConflictError("Confirmation does not match the database name")
    await mariadb.drop_database(row.name, row.db_user)
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{database_id}/reset-password", response_model=CredentialsResponse)
async def reset_password(database_id: int, db: AsyncSession = Depends(get_db)) -> Any:
    row = await db.get(Database, database_id)
    if row is None:
        raise NotFoundError("Database not found")
    password = mariadb.generate_password()
    await mariadb.reset_password(row.db_user, password)
    row.password_hash = hash_token(password)
    await db.commit()
    await db.refresh(row)
    return CredentialsResponse(database=DatabaseResponse.model_validate(row), password=password)


@router.post("/adminer-session", response_model=AdminerSessionResponse)
async def adminer_session(request: Request) -> Any:
    """Mint a short-lived ticket; the /adminer proxy swaps it for a cookie."""
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

    response = Response(
        content=upstream_resp.content,
        status_code=upstream_resp.status_code,
        headers={k: v for k, v in upstream_resp.headers.items() if k.lower() not in HOP_BY_HOP},
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
