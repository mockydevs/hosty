"""Authentication: first-boot setup, login, refresh rotation, logout, password change.

See docs/ARCHITECTURE.md ADR-004 for the token design.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.errors import ConflictError, RateLimitedError, UnauthorizedError
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.db.models import RefreshToken, User

router = APIRouter()

REFRESH_COOKIE = "hosty_refresh"
REFRESH_COOKIE_PATH = "/api/auth"

PASSWORD_MIN = 12
PASSWORD_MAX = 128


class SetupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]*$")
    password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=PASSWORD_MIN, max_length=PASSWORD_MAX)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    username: str
    role: str


class SetupStatusResponse(BaseModel):
    setup_required: bool


def _settings(request: Request) -> Settings:
    return request.app.state.settings


async def _user_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User))).scalar_one()


async def _issue_tokens(
    request: Request, response: Response, db: AsyncSession, user: User
) -> TokenResponse:
    settings = _settings(request)
    access = create_access_token(
        subject=str(user.id),
        secret=settings.secret_key,
        ttl_seconds=settings.access_token_ttl_seconds,
    )
    raw_refresh = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(raw_refresh),
            expires_at=utcnow() + timedelta(seconds=settings.refresh_token_ttl_seconds),
        )
    )
    await db.commit()
    response.set_cookie(
        REFRESH_COOKIE,
        raw_refresh,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path=REFRESH_COOKIE_PATH,
    )
    return TokenResponse(access_token=access, expires_in=settings.access_token_ttl_seconds)


@router.get("/setup", response_model=SetupStatusResponse)
async def setup_status(db: AsyncSession = Depends(get_db)) -> SetupStatusResponse:
    return SetupStatusResponse(setup_required=await _user_count(db) == 0)


@router.post("/setup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def setup(body: SetupRequest, db: AsyncSession = Depends(get_db)) -> User:
    """Create the admin account. Open only while no users exist (first boot)."""
    if await _user_count(db) > 0:
        raise ConflictError("Setup has already been completed")
    user = User(username=body.username, password_hash=hash_password(body.password), role="admin")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    limiter = request.app.state.login_limiter
    key = request.client.host if request.client else "unknown"
    if not limiter.allow(key):
        raise RateLimitedError("Too many login attempts; try again later")
    user = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if user is None or not verify_password(user.password_hash, body.password):
        raise UnauthorizedError("Invalid username or password")
    limiter.reset(key)
    return await _issue_tokens(request, response, db, user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise UnauthorizedError("Missing refresh token")
    now = utcnow()
    row = (
        await db.execute(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw)))
    ).scalar_one_or_none()
    if row is None:
        raise UnauthorizedError("Invalid refresh token")
    if row.revoked_at is not None:
        # Reuse of a rotated token => assume theft, revoke the whole family.
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == row.user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await db.commit()
        raise UnauthorizedError("Refresh token reuse detected; all sessions revoked")
    if row.expires_at < now:
        raise UnauthorizedError("Refresh token expired")
    user = await db.get(User, row.user_id)
    if user is None:
        raise UnauthorizedError("User no longer exists")
    row.revoked_at = now
    return await _issue_tokens(request, response, db, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> None:
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.token_hash == hash_token(raw))
            .values(revoked_at=utcnow())
        )
        await db.commit()
    response.delete_cookie(REFRESH_COOKIE, path=REFRESH_COOKIE_PATH)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    if not verify_password(user.password_hash, body.current_password):
        raise UnauthorizedError("Current password is incorrect")
    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = utcnow()
    db.add(user)
    # Revoke every active session: old access tokens die via iat check.
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await db.commit()


@router.get("/me", response_model=UserResponse)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
