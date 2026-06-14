"""Global tags management."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.api.deps import get_db, require_admin
from app.core.errors import NotFoundError, ConflictError
from app.db.models import Tag, StackTag

router = APIRouter()

class GlobalTagResponse(BaseModel):
    id: int
    name: str
    usage_count: int

@router.get("/", response_model=List[GlobalTagResponse])
async def list_global_tags(
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin)
) -> List[GlobalTagResponse]:
    # Query to get tags and count of associated stacks
    stmt = (
        select(Tag, func.count(StackTag.stack_id).label("usage_count"))
        .outerjoin(StackTag, Tag.id == StackTag.tag_id)
        .group_by(Tag.id)
        .order_by(Tag.name)
    )
    result = await db.execute(stmt)
    rows = result.all()
    
    return [
        GlobalTagResponse(
            id=tag.id,
            name=tag.name,
            usage_count=count
        )
        for tag, count in rows
    ]

@router.delete("/{tag_id}")
async def delete_global_tag(
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin)
):
    tag = await db.get(Tag, tag_id)
    if not tag:
        raise NotFoundError("Tag not found.")
    
    await db.delete(tag)
    await db.commit()
    return {"ok": True}
