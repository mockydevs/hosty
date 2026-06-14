"""Shared environment variables management."""

import structlog
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from app.api.deps import get_db, require_admin
from app.core.errors import ConflictError, NotFoundError
from app.core.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret
from app.db.models import SharedVariable

log = structlog.get_logger("hosty.api.shared_variables")

router = APIRouter()

class SharedVariableCreate(BaseModel):
    key: str = Field(..., max_length=255, pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$")
    value: str = Field(..., max_length=4096)
    description: str | None = Field(None, max_length=255)

class SharedVariableUpdate(BaseModel):
    value: str | None = Field(None, max_length=4096)
    description: str | None = Field(None, max_length=255)

class SharedVariableResponse(BaseModel):
    id: int
    key: str
    value: str
    description: str | None
    created_at: datetime

@router.get("/", response_model=list[SharedVariableResponse])
async def list_shared_variables(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin)
) -> list[SharedVariableResponse]:
    settings = request.app.state.settings
    result = await db.execute(select(SharedVariable).order_by(SharedVariable.key))
    rows = result.scalars().all()
    out = []
    for var in rows:
        try:
            value = decrypt_secret(var.value, settings.secret_key)
        except (SecretDecryptionError, Exception):
            log.warning("shared_variable_decrypt_failed", key=var.key)
            value = var.value  # fallback: return as-is (pre-existing plaintext)
        out.append(SharedVariableResponse(
            id=var.id,
            key=var.key,
            value=value,
            description=var.description,
            created_at=var.created_at,
        ))
    return out

@router.post("/", response_model=SharedVariableResponse)
async def create_shared_variable(
    request: Request,
    var_in: SharedVariableCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin)
) -> SharedVariableResponse:
    settings = request.app.state.settings
    existing = await db.scalar(select(SharedVariable).where(SharedVariable.key == var_in.key))
    if existing:
        raise ConflictError(f"Shared variable '{var_in.key}' already exists.")

    data = var_in.model_dump()
    data["value"] = encrypt_secret(var_in.value, settings.secret_key)
    var = SharedVariable(**data)
    db.add(var)
    await db.commit()
    await db.refresh(var)
    return SharedVariableResponse(
        id=var.id,
        key=var.key,
        value=var_in.value,  # return the plaintext value the caller just sent
        description=var.description,
        created_at=var.created_at,
    )

@router.patch("/{var_id}", response_model=SharedVariableResponse)
async def update_shared_variable(
    var_id: int,
    request: Request,
    var_in: SharedVariableUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin)
) -> SharedVariableResponse:
    settings = request.app.state.settings
    var = await db.get(SharedVariable, var_id)
    if not var:
        raise NotFoundError("Shared variable not found.")

    data = var_in.model_dump(exclude_unset=True)
    plaintext_value: str | None = None
    if "value" in data:
        plaintext_value = data["value"]
        data["value"] = encrypt_secret(plaintext_value, settings.secret_key)
    for k, v in data.items():
        setattr(var, k, v)

    await db.commit()
    await db.refresh(var)

    # Decrypt the stored value for the response
    if plaintext_value is not None:
        decrypted = plaintext_value  # we just set it, no need to decrypt again
    else:
        try:
            decrypted = decrypt_secret(var.value, settings.secret_key)
        except (SecretDecryptionError, Exception):
            log.warning("shared_variable_decrypt_failed", key=var.key)
            decrypted = var.value  # fallback: return as-is (pre-existing plaintext)

    return SharedVariableResponse(
        id=var.id,
        key=var.key,
        value=decrypted,
        description=var.description,
        created_at=var.created_at,
    )

@router.delete("/{var_id}")
async def delete_shared_variable(
    var_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin)
):
    var = await db.get(SharedVariable, var_id)
    if not var:
        raise NotFoundError("Shared variable not found.")
    
    await db.delete(var)
    await db.commit()
    return {"ok": True}
