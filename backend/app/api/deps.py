"""Shared FastAPI dependencies: database session, authenticated user, and the
Phase 11a authorization layer (admin gate + owner-scoped resource fetch)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utc_timestamp
from app.core.errors import AppError, ForbiddenError, NotFoundError, UnauthorizedError
from app.core.security import decode_access_token
from app.db.models import Site, User

_bearer = HTTPBearer(auto_error=False)

# While a temp password is in force, only these endpoints work (the UI shows a
# forced change-password screen; everything else returns 403).
_PASSWORD_CHANGE_ALLOWED_PATHS = frozenset(
    {"/api/auth/change-password", "/api/auth/me", "/api/auth/logout"}
)


class PasswordChangeRequiredError(AppError):
    status_code = 403
    code = "password_change_required"


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise UnauthorizedError("Not authenticated")
    settings = request.app.state.settings
    payload = decode_access_token(credentials.credentials, secret=settings.secret_key)
    user = await db.get(User, int(payload["sub"]))
    if user is None:
        raise UnauthorizedError("User no longer exists")
    # Tokens issued before the last password change are invalid.
    if int(payload["iat"]) < utc_timestamp(user.password_changed_at):
        raise UnauthorizedError("Token is no longer valid")
    if user.suspended:
        raise UnauthorizedError("Account suspended — contact your administrator")
    if user.must_change_password and request.url.path not in _PASSWORD_CHANGE_ALLOWED_PATHS:
        raise PasswordChangeRequiredError("Change your temporary password to continue")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Gate for admin-only routers/endpoints (users, DNS, system mutations…)."""
    if user.role != "admin":
        raise ForbiddenError("Administrator access required")
    return user


def is_admin(user: User) -> bool:
    return user.role == "admin"


async def fetch_owned_site(db: AsyncSession, user: User, site_id: int) -> Site:
    """Load a site the user may act on. Admins see everything; clients get a
    404 (not 403) for other tenants' sites so existence never leaks."""
    site = await db.get(Site, site_id)
    if site is None or (not is_admin(user) and site.owner_id != user.id):
        raise NotFoundError("Site not found")
    return site
