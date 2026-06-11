"""Shared FastAPI dependencies: database session and authenticated user."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utc_timestamp
from app.core.errors import UnauthorizedError
from app.core.security import decode_access_token
from app.db.models import User

_bearer = HTTPBearer(auto_error=False)


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
    return user
