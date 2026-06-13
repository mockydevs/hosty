"""Global inbound webhook handlers — GitHub App push events routed to matching stacks."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.core.errors import ConflictError
from app.core.secrets import decrypt_secret
from app.db.models import GitSource, Operation, Stack

router = APIRouter()


@router.post("/github", status_code=status.HTTP_202_ACCEPTED)
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Receives GitHub App push events and triggers rebuilds for matching stacks."""
    payload = await request.body()
    event = request.headers.get("x-github-event", "")

    if event == "ping":
        return {"status": "ok"}

    if event != "push":
        return {"status": "ignored", "event": event}

    try:
        body = json.loads(payload)
    except Exception:
        raise ConflictError("Invalid JSON payload")

    installation_id = str(body.get("installation", {}).get("id", ""))
    ref = body.get("ref", "")
    branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ""
    clone_url: str = body.get("repository", {}).get("clone_url", "")

    if not branch or not installation_id or not clone_url:
        return {"status": "ignored", "reason": "missing push metadata"}

    result = await db.execute(
        select(GitSource).where(GitSource.installation_id == installation_id)
    )
    source = result.scalar_one_or_none()
    if source is None:
        return {"status": "ignored", "reason": "no matching source"}

    settings = request.app.state.settings
    webhook_secret = decrypt_secret(source.webhook_secret_encrypted, settings.secret_key)
    sig_header = request.headers.get("x-hub-signature-256", "")
    expected = "sha256=" + hmac.new(
        webhook_secret.encode(), payload, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig_header):
        raise ConflictError("Invalid webhook signature")

    stacks_result = await db.execute(
        select(Stack).where(Stack.blueprint_id == "git", Stack.status != "deleting")
    )
    all_stacks = stacks_result.scalars().all()

    matched: list[tuple[Stack, Operation]] = []
    for stack in all_stacks:
        if not stack.inputs_encrypted:
            continue
        try:
            inputs = json.loads(decrypt_secret(stack.inputs_encrypted, settings.secret_key))
        except Exception:
            continue

        if str(inputs.get("source_id", "")) != str(source.id):
            continue
        if inputs.get("branch", "main") != branch:
            continue

        # Normalise both sides: strip trailing slash and .git suffix
        stack_repo = inputs.get("repo", "").rstrip("/").removesuffix(".git")
        push_repo = clone_url.rstrip("/").removesuffix(".git")
        if stack_repo != push_repo:
            continue

        stack.generation += 1
        op = Operation(kind="webhook_rebuild", stack_id=stack.id, domain=stack.name)
        db.add(op)
        matched.append((stack, op))

    if not matched:
        return {"status": "ignored", "reason": "no matching stacks"}

    await db.commit()
    for stack_obj, op_obj in matched:
        await db.refresh(op_obj)
        background.add_task(
            request.app.state.reconciler.converge_stack,
            stack_obj.name,
            operation_id=op_obj.id,
        )

    return {"status": "accepted", "triggered": len(matched)}
