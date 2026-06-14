"""Shared environment variables management."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from app.api.deps import get_db, require_admin
from app.core.errors import ConflictError, NotFoundError
from app.db.models import SharedVariable

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
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin)
) -> list[SharedVariableResponse]:
    result = await db.execute(select(SharedVariable).order_by(SharedVariable.key))
    return list(result.scalars().all())

@router.post("/", response_model=SharedVariableResponse)
async def create_shared_variable(
    var_in: SharedVariableCreate,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin)
) -> SharedVariableResponse:
    existing = await db.scalar(select(SharedVariable).where(SharedVariable.key == var_in.key))
    if existing:
        raise ConflictError(f"Shared variable '{var_in.key}' already exists.")
    
    var = SharedVariable(**var_in.model_dump())
    db.add(var)
    await db.commit()
    await db.refresh(var)
    return var

@router.patch("/{var_id}", response_model=SharedVariableResponse)
async def update_shared_variable(
    var_id: int,
    var_in: SharedVariableUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin)
) -> SharedVariableResponse:
    var = await db.get(SharedVariable, var_id)
    if not var:
        raise NotFoundError("Shared variable not found.")
    
    data = var_in.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(var, k, v)
        
    await db.commit()
    await db.refresh(var)
    return var

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
