import asyncio
import hashlib
import secrets as py_secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.errors import ConflictError, NotFoundError
from app.core.secrets import decrypt_secret, encrypt_secret
from app.db.models import ApiToken, SshKey, Tenant, User
from app.system import stackhost

router = APIRouter()


# --- SSH Keys ---------------------------------------------------------------------

class SshKeyCreate(BaseModel):
    name: str = Field(..., max_length=64, pattern=r"^[a-z0-9-]+$")
    private_key: str = Field(..., min_length=1)
    public_key: str = Field(..., min_length=1)


class SshKeyResponse(BaseModel):
    id: int
    name: str
    public_key: str
    created_at: datetime


def _settings(request: Request):
    return request.app.state.settings


async def _sync_tenant_keys(db: AsyncSession, user_id: int, secret_key: str) -> None:
    tenant = (
        await db.execute(select(Tenant).where(Tenant.user_id == user_id))
    ).scalar_one_or_none()
    if not tenant or tenant.uid is None:
        return

    keys = (await db.execute(select(SshKey).where(SshKey.owner_id == user_id))).scalars().all()
    key_tuples = [(k.name, decrypt_secret(k.private_key_encrypted, secret_key)) for k in keys]
    await asyncio.to_thread(stackhost.sync_ssh_keys, tenant.linux_user, key_tuples)


@router.get("/keys", response_model=list[SshKeyResponse])
async def list_ssh_keys(
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """List SSH keys for the current user."""
    keys = (
        await db.execute(select(SshKey).where(SshKey.owner_id == current_user.id))
    ).scalars().all()
    return [
        SshKeyResponse(
            id=k.id,
            name=k.name,
            public_key=k.public_key,
            created_at=k.created_at,
        )
        for k in keys
    ]


@router.post("/keys", response_model=SshKeyResponse)
async def create_ssh_key(
    request: Request,
    req: SshKeyCreate,
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Add a new SSH private key."""
    existing = (
        await db.execute(
            select(SshKey).where(
                SshKey.owner_id == current_user.id,
                SshKey.name == req.name,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise ConflictError("Key name already exists")

    key = SshKey(
        owner_id=current_user.id,
        name=req.name,
        public_key=req.public_key,
        private_key_encrypted=encrypt_secret(req.private_key, _settings(request).secret_key),
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)

    await _sync_tenant_keys(db, current_user.id, _settings(request).secret_key)

    return SshKeyResponse(
        id=key.id,
        name=key.name,
        public_key=key.public_key,
        created_at=key.created_at,
    )


@router.delete("/keys/{key_id}")
async def delete_ssh_key(
    request: Request,
    key_id: int,
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Delete an SSH key."""
    key = await db.get(SshKey, key_id)
    if not key or key.owner_id != current_user.id:
        raise NotFoundError("Key not found")

    await db.delete(key)
    await db.commit()

    await _sync_tenant_keys(db, current_user.id, _settings(request).secret_key)
    return {"status": "ok"}


# --- API Tokens -------------------------------------------------------------------

class ApiTokenCreate(BaseModel):
    name: str = Field(..., max_length=64)


class ApiTokenResponse(BaseModel):
    id: int
    name: str
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None


@router.get("/tokens", response_model=list[ApiTokenResponse])
async def list_api_tokens(
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """List API tokens."""
    tokens = (
        await db.execute(select(ApiToken).where(ApiToken.owner_id == current_user.id))
    ).scalars().all()
    return [
        ApiTokenResponse(
            id=t.id,
            name=t.name,
            created_at=t.created_at,
            last_used_at=t.last_used_at,
            expires_at=t.expires_at,
        )
        for t in tokens
    ]


@router.post("/tokens")
async def create_api_token(
    req: ApiTokenCreate,
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Generate a new API token. The raw token is only returned once."""
    raw_token = py_secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    token = ApiToken(
        owner_id=current_user.id,
        name=req.name,
        token_hash=token_hash,
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)

    return {
        "id": token.id,
        "name": token.name,
        "token": f"hst_{raw_token}",
        "created_at": token.created_at,
    }


@router.delete("/tokens/{token_id}")
async def revoke_api_token(
    token_id: int,
    db: AsyncSession = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_user),
):
    """Revoke an API token."""
    token = await db.get(ApiToken, token_id)
    if not token or token.owner_id != current_user.id:
        raise NotFoundError("Token not found")

    await db.delete(token)
    await db.commit()
    return {"status": "ok"}
