from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import httpx
import jwt as pyjwt
from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.core.errors import ConflictError, NotFoundError
from app.core.secrets import decrypt_secret, encrypt_secret
from app.db.models import GitSource, User

router = APIRouter()


class SourceResponse(BaseModel):
    id: int
    name: str
    provider: str
    app_id: str
    installation_id: str | None
    created_at: datetime


class GitHubCallbackBody(BaseModel):
    code: str
    installation_id: str | None = None
    name: str | None = None


@router.get("", response_model=list[SourceResponse])
async def list_sources(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    result = await db.execute(select(GitSource).where(GitSource.owner_id == user.id))
    return result.scalars().all()


@router.get("/{source_id}", response_model=SourceResponse)
async def get_source(
    source_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    source = await db.get(GitSource, source_id)
    if not source or source.owner_id != user.id:
        raise NotFoundError("Source not found")
    return source


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source(
    source_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    source = await db.get(GitSource, source_id)
    if not source or source.owner_id != user.id:
        raise NotFoundError("Source not found")
    await db.delete(source)
    await db.commit()


@router.post("/github/manifest")
async def github_manifest(
    request: Request,
    user: User = Depends(get_current_user),
) -> Any:
    """Returns the manifest JSON for creating a GitHub App via the manifest flow."""
    host = request.headers.get("host", "localhost:8000")
    protocol = "https" if "localhost" not in host else "http"
    base_url = f"{protocol}://{host}"

    manifest = {
        "name": f"Hosty ({user.username})",
        "url": base_url,
        "hook_attributes": {
            "url": f"{base_url}/api/webhooks/github",
            "active": True,
        },
        "redirect_url": f"{base_url}/sources/github/callback",
        "public": False,
        "default_permissions": {
            "contents": "read",
            "metadata": "read",
            "pull_requests": "read",
        },
        "default_events": ["push", "pull_request"],
    }

    return {"manifest": manifest}


@router.post("/github/callback")
async def github_callback(
    body: GitHubCallbackBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
) -> Any:
    """Exchange a GitHub App manifest code for credentials and persist the GitSource."""
    settings = request.app.state.settings

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.github.com/app-manifests/{body.code}/conversions",
            headers={"Accept": "application/vnd.github.v3+json"},
        )

    if resp.status_code != 201:
        raise ConflictError(f"GitHub App creation failed ({resp.status_code}): {resp.text[:200]}")

    data = resp.json()
    app_name = body.name or data.get("slug") or data.get("name") or "github-app"

    source = GitSource(
        owner_id=user.id,
        name=app_name,
        provider="github",
        app_id=str(data["id"]),
        installation_id=body.installation_id,
        client_id=data["client_id"],
        client_secret_encrypted=encrypt_secret(data["client_secret"], settings.secret_key),
        private_key_encrypted=encrypt_secret(data["pem"], settings.secret_key),
        webhook_secret_encrypted=encrypt_secret(data["webhook_secret"], settings.secret_key),
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return {"status": "ok", "source_id": source.id}


@router.get("/{source_id}/repos")
async def list_source_repos(
    source_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
) -> Any:
    """List repositories accessible to the GitHub App installation."""
    source = await db.get(GitSource, source_id)
    if not source or source.owner_id != user.id:
        raise NotFoundError("Source not found")

    if not source.installation_id:
        raise ConflictError("This source has no installation ID — the GitHub App may not be installed yet.")

    settings = request.app.state.settings
    private_key = decrypt_secret(source.private_key_encrypted, settings.secret_key)

    now = int(time.time())
    app_jwt = pyjwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": source.app_id},
        private_key,
        algorithm="RS256",
    )

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            f"https://api.github.com/app/installations/{source.installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github.v3+json",
            },
        )
        if token_resp.status_code != 201:
            raise ConflictError(f"Could not obtain installation token: {token_resp.text[:200]}")
        access_token = token_resp.json()["token"]

        repos_resp = await client.get(
            "https://api.github.com/installation/repositories",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github.v3+json",
            },
            params={"per_page": 100},
        )
        if repos_resp.status_code != 200:
            raise ConflictError(f"Could not list repositories: {repos_resp.text[:200]}")

    repos = repos_resp.json().get("repositories", [])
    return [
        {
            "name": r["full_name"],
            "clone_url": r["clone_url"],
            "default_branch": r.get("default_branch", "main"),
        }
        for r in repos
    ]
