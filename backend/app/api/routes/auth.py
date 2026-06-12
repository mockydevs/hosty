"""Authentication: first-boot setup, login, refresh rotation, logout, password change.

See docs/ARCHITECTURE.md ADR-004 for the token design.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core import totp as totp_lib
from app.core.clock import utcnow
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError, RateLimitedError, UnauthorizedError
from app.core.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret
from app.core.security import (
    create_access_token,
    create_totp_challenge,
    decode_totp_challenge,
    generate_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.db.models import RefreshToken, SetupState, User

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


class LoginResponse(BaseModel):
    """Either tokens (no 2FA) or a short-lived challenge requiring a TOTP code."""

    totp_required: bool = False
    challenge_token: str | None = None
    access_token: str | None = None
    token_type: str = "bearer"
    expires_in: int | None = None


class TotpVerifyRequest(BaseModel):
    challenge_token: str
    code: str = Field(min_length=6, max_length=8)


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    username: str
    role: str
    must_change_password: bool
    totp_enabled: bool = False
    impersonated_by: str | None = None


class SetupStatusResponse(BaseModel):
    setup_required: bool


def _settings(request: Request) -> Settings:
    return request.app.state.settings


async def _user_count(db: AsyncSession) -> int:
    return (await db.execute(select(func.count()).select_from(User))).scalar_one()


async def _issue_tokens(
    request: Request,
    response: Response,
    db: AsyncSession,
    user: User,
    *,
    family_id: str | None = None,
) -> TokenResponse:
    settings = _settings(request)
    access = create_access_token(
        subject=str(user.id),
        secret=settings.secret_key,
        ttl_seconds=settings.access_token_ttl_seconds,
        extra_claims={"ver": user.token_version},
    )
    raw_refresh = generate_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=hash_token(raw_refresh),
            family_id=family_id or secrets.token_hex(32),
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
    claimed = await db.get(SetupState, 1)
    return SetupStatusResponse(setup_required=claimed is None and await _user_count(db) == 0)


@router.post("/setup", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def setup(body: SetupRequest, db: AsyncSession = Depends(get_db)) -> User:
    """Create the admin account. Open only while no users exist (first boot)."""
    if await _user_count(db) > 0:
        raise ConflictError("Setup has already been completed")
    user = User(username=body.username, password_hash=hash_password(body.password), role="admin")
    db.add(SetupState(id=1, completed_at=utcnow()))
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("Setup has already been completed") from exc
    await db.refresh(user)
    return user


@router.post("/login", response_model=LoginResponse, response_model_exclude_none=True)
async def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
) -> LoginResponse:
    limiter = request.app.state.login_limiter
    key = request.client.host if request.client else "unknown"
    if not limiter.allow(key):
        raise RateLimitedError("Too many login attempts; try again later")
    user = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if user is None or not verify_password(user.password_hash, body.password):
        raise UnauthorizedError("Invalid username or password")
    if user.suspended:
        raise UnauthorizedError("Account suspended — contact your administrator")
    settings = _settings(request)
    if user.totp_enabled:
        # Password OK, but tokens are only issued after a valid TOTP code.
        # The limiter is NOT reset: failed codes still count against the IP.
        return LoginResponse(
            totp_required=True,
            challenge_token=create_totp_challenge(subject=str(user.id), secret=settings.secret_key),
        )
    limiter.reset(key)
    tokens = await _issue_tokens(request, response, db, user)
    return LoginResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


@router.post("/login/totp", response_model=TokenResponse)
async def login_totp(
    request: Request,
    response: Response,
    body: TotpVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Second login step when 2FA is enabled: challenge token + TOTP code → tokens."""
    limiter = request.app.state.login_limiter
    key = request.client.host if request.client else "unknown"
    if not limiter.allow(key):
        raise RateLimitedError("Too many login attempts; try again later")
    settings = _settings(request)
    payload = decode_totp_challenge(body.challenge_token, secret=settings.secret_key)
    user = await db.get(User, int(payload["sub"]))
    if user is None or user.suspended or not user.totp_enabled or not user.totp_secret_encrypted:
        raise UnauthorizedError("Invalid 2FA challenge")
    try:
        secret = decrypt_secret(user.totp_secret_encrypted, settings.secret_key)
    except SecretDecryptionError as exc:
        raise UnauthorizedError("2FA secret unreadable — contact your administrator") from exc
    if not totp_lib.verify(secret, body.code):
        raise UnauthorizedError("Invalid authentication code")
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
            .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await db.commit()
        raise UnauthorizedError("Refresh token reuse detected; all sessions revoked")
    if row.expires_at < now:
        raise UnauthorizedError("Refresh token expired")
    user = await db.get(User, row.user_id)
    if user is None:
        raise UnauthorizedError("User no longer exists")
    if user.suspended:
        raise UnauthorizedError("Account suspended — contact your administrator")
    claimed = await db.execute(
        update(RefreshToken)
        .where(RefreshToken.id == row.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    if claimed.rowcount != 1:
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await db.commit()
        raise UnauthorizedError("Refresh token reuse detected; session family revoked")
    return await _issue_tokens(request, response, db, user, family_id=row.family_id)


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
    user.must_change_password = False  # temp password fulfilled (Phase 11a)
    user.password_changed_at = utcnow()
    user.token_version += 1
    db.add(user)
    # Revoke every active session; token_version invalidates access tokens immediately.
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await db.commit()


@router.get("/me", response_model=UserResponse)
async def me(request: Request, user: User = Depends(get_current_user)) -> UserResponse:
    impersonator = getattr(request.state, "impersonator", None)
    return UserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        must_change_password=user.must_change_password,
        totp_enabled=user.totp_enabled,
        impersonated_by=impersonator.username if impersonator is not None else None,
    )


# --- 2FA (TOTP, Phase 11d) ---------------------------------------------------------


class TotpSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str


class TotpEnableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=8)


class TotpDisableRequest(BaseModel):
    password: str


@router.post("/2fa/setup", response_model=TotpSetupResponse)
async def totp_setup(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TotpSetupResponse:
    """Generate (or regenerate) a TOTP secret. 2FA only takes effect after the
    first valid code is submitted to /2fa/enable."""
    if user.totp_enabled:
        raise ConflictError("2FA is already enabled — disable it first to re-enroll")
    secret = totp_lib.generate_secret()
    user.totp_secret_encrypted = encrypt_secret(secret, _settings(request).secret_key)
    await db.commit()
    return TotpSetupResponse(
        secret=secret, otpauth_uri=totp_lib.otpauth_uri(secret, username=user.username)
    )


@router.post("/2fa/enable", status_code=status.HTTP_204_NO_CONTENT)
async def totp_enable(
    request: Request,
    body: TotpEnableRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    if user.totp_enabled:
        raise ConflictError("2FA is already enabled")
    if not user.totp_secret_encrypted:
        raise ConflictError("Run 2FA setup first")
    secret = decrypt_secret(user.totp_secret_encrypted, _settings(request).secret_key)
    if not totp_lib.verify(secret, body.code):
        raise UnauthorizedError("Invalid authentication code — check your authenticator app")
    user.totp_enabled = True
    await db.commit()


@router.post("/2fa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def totp_disable(
    body: TotpDisableRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Password-gated: a stolen session alone must not be able to remove 2FA."""
    if not verify_password(user.password_hash, body.password):
        raise UnauthorizedError("Password is incorrect")
    user.totp_enabled = False
    user.totp_secret_encrypted = None
    await db.commit()


# --- active sessions (Phase 11d) -----------------------------------------------------


class SessionResponse(BaseModel):
    id: int
    created_at: datetime
    expires_at: datetime
    current: bool


@router.get("/sessions", response_model=list[SessionResponse])
async def list_sessions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[SessionResponse]:
    """The user's active (unexpired, unrevoked) refresh-token sessions."""
    current_hash = None
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        current_hash = hash_token(raw)
    rows = (
        (
            await db.execute(
                select(RefreshToken)
                .where(
                    RefreshToken.user_id == user.id,
                    RefreshToken.revoked_at.is_(None),
                    RefreshToken.expires_at > utcnow(),
                )
                .order_by(RefreshToken.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        SessionResponse(
            id=row.id,
            created_at=row.created_at,
            expires_at=row.expires_at,
            current=row.token_hash == current_hash,
        )
        for row in rows
    ]


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    row = await db.get(RefreshToken, session_id)
    if row is None or row.user_id != user.id or row.revoked_at is not None:
        raise NotFoundError("Session not found")
    row.revoked_at = utcnow()
    await db.commit()


@router.post("/sessions/revoke-others", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_other_sessions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    """Revoke every session except the one backing this browser's cookie."""
    current_hash = hash_token(request.cookies.get(REFRESH_COOKIE, ""))
    await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.token_hash != current_hash,
        )
        .values(revoked_at=utcnow())
    )
    await db.commit()
