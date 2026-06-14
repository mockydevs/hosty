import os

filepath = "backend/app/api/routes/stacks.py"
with open(filepath, "r", encoding="utf-8") as f:
    content = f.read()

# Make sure imports are added at the top
if "ScheduledTask" not in content:
    content = content.replace("from app.db.models import App, GitSource, Operation, Site, Stack, StackEndpoint, User",
                              "from app.db.models import App, GitSource, Operation, Site, Stack, StackEndpoint, User, ScheduledTask, Tag, StackTag, Deployment")

if "ServerResponse" not in content:
    content = content.replace("from pydantic import BaseModel, Field, ValidationError",
                              "from pydantic import BaseModel, Field, ValidationError\nfrom app.api.routes.servers import ServerResponse")

# Add server to StackResponse
content = content.replace("server_id: int | None = None", "server_id: int | None = None\n    server: ServerResponse | None = None")

append_str = """
# ==============================================================================
# COOLIFY-STYLE MOCK ENDPOINTS (Tags, Scheduled Tasks, Deployments, Metrics, etc)
# ==============================================================================

class ConfigUpdateRequest(BaseModel):
    repo: str | None = None
    branch: str | None = None
    auto_deploy: bool | None = None
    force_rebuild: bool | None = None

@router.patch("/{stack_id}/config")
async def update_stack_config(
    stack_id: int,
    body: ConfigUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    stack = await db.scalar(select(Stack).where(Stack.id == stack_id))
    if not stack:
        raise NotFoundError("Stack not found")
    if not is_admin(user) and stack.owner_id != user.id:
        raise NotFoundError("Stack not found")

    import json
    from app.core.secrets import encrypt_secret, decrypt_secret
    
    inputs = {}
    if stack.inputs_encrypted:
        inputs = json.loads(decrypt_secret(stack.inputs_encrypted))
    
    if body.repo is not None:
        inputs['repo'] = body.repo
    if body.branch is not None:
        inputs['branch'] = body.branch
    if body.auto_deploy is not None:
        inputs['auto_deploy'] = body.auto_deploy
    if body.force_rebuild is not None:
        inputs['force_rebuild'] = body.force_rebuild
        
    stack.inputs_encrypted = encrypt_secret(json.dumps(inputs))
    await db.commit()
    return {"status": "ok"}


@router.get("/{stack_id}/tags")
async def get_stack_tags(stack_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Tag).join(StackTag).where(StackTag.stack_id == stack_id)
    )
    return [{"id": t.id, "name": t.name} for t in result.scalars()]


class TagCreateRequest(BaseModel):
    name: str

@router.post("/{stack_id}/tags")
async def add_stack_tag(stack_id: int, body: TagCreateRequest, db: AsyncSession = Depends(get_db)):
    tag = await db.scalar(select(Tag).where(Tag.name == body.name))
    if not tag:
        tag = Tag(name=body.name)
        db.add(tag)
        await db.flush()
    
    existing = await db.scalar(select(StackTag).where(StackTag.stack_id == stack_id, StackTag.tag_id == tag.id))
    if not existing:
        db.add(StackTag(stack_id=stack_id, tag_id=tag.id))
        await db.commit()
    return {"status": "ok", "tag": {"id": tag.id, "name": tag.name}}


@router.delete("/{stack_id}/tags/{tag_id}")
async def remove_stack_tag(stack_id: int, tag_id: int, db: AsyncSession = Depends(get_db)):
    st = await db.scalar(select(StackTag).where(StackTag.stack_id == stack_id, StackTag.tag_id == tag_id))
    if st:
        await db.delete(st)
        await db.commit()
    return {"status": "ok"}


@router.get("/{stack_id}/scheduled-tasks")
async def get_scheduled_tasks(stack_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ScheduledTask).where(ScheduledTask.stack_id == stack_id))
    return [
        {"id": t.id, "name": t.name, "command": t.command, "cron_schedule": t.cron_schedule}
        for t in result.scalars()
    ]


class ScheduledTaskCreate(BaseModel):
    name: str
    command: str
    cron_schedule: str

@router.post("/{stack_id}/scheduled-tasks")
async def add_scheduled_task(stack_id: int, body: ScheduledTaskCreate, db: AsyncSession = Depends(get_db)):
    task = ScheduledTask(stack_id=stack_id, name=body.name, command=body.command, cron_schedule=body.cron_schedule)
    db.add(task)
    await db.commit()
    return {"status": "ok"}


@router.delete("/{stack_id}/scheduled-tasks/{task_id}")
async def remove_scheduled_task(stack_id: int, task_id: int, db: AsyncSession = Depends(get_db)):
    task = await db.scalar(select(ScheduledTask).where(ScheduledTask.id == task_id))
    if task:
        await db.delete(task)
        await db.commit()
    return {"status": "ok"}


@router.get("/{stack_id}/deployments")
async def get_deployments(stack_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Deployment).where(Deployment.stack_id == stack_id).order_by(Deployment.created_at.desc()))
    return [
        {"id": d.id, "commit_sha": d.commit_sha, "status": d.status, "message": d.message, "created_at": d.created_at.isoformat()}
        for d in result.scalars()
    ]


@router.post("/{stack_id}/deployments/{deployment_id}/rollback")
async def rollback_deployment(stack_id: int, deployment_id: int, request: Request, background: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    deployment = await db.scalar(select(Deployment).where(Deployment.id == deployment_id))
    if not deployment:
        raise NotFoundError("Deployment not found")
    
    stack = await db.scalar(select(Stack).where(Stack.id == stack_id))
    stack.generation += 1
    op = Operation(kind="converge_stack", stack_id=stack.id, domain=stack.name)
    db.add(op)
    
    new_dep = Deployment(stack_id=stack_id, commit_sha=deployment.commit_sha, status="running", message=f"Rollback to {deployment.commit_sha}")
    db.add(new_dep)
    
    await db.commit()
    background.add_task(request.app.state.reconciler.converge_stack, stack.name, operation_id=op.id)
    return {"status": "ok", "operation_id": op.id}


@router.get("/{stack_id}/webhook_info")
async def get_webhook_info(stack_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    import hashlib
    return {
        "url": f"{request.base_url}api/stacks/{stack_id}/webhook",
        "secret": "whsec_" + hashlib.sha256(str(stack_id).encode()).hexdigest()[:16]
    }


@router.get("/{stack_id}/metrics")
async def get_stack_metrics(stack_id: int):
    import random
    from datetime import datetime, timedelta
    
    now = datetime.utcnow()
    metrics = []
    for i in range(24):
        time = now - timedelta(hours=24-i)
        metrics.append({
            "time": time.strftime("%H:%M"),
            "cpu": random.uniform(0.5, 5.0),
            "memory": random.uniform(100, 500)
        })
    return metrics
"""

if "update_stack_config" not in content:
    content += append_str

with open(filepath, "w", encoding="utf-8") as f:
    f.write(content)
print("Updated stacks.py")
