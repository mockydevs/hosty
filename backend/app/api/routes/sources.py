from __future__ import annotations

import base64
import hashlib
import hmac
import json
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
from app.core.secrets import SecretDecryptionError, decrypt_secret, encrypt_secret
from app.db.models import GitSource, User

router = APIRouter()


def _make_install_state(source_id: int, secret_key: str) -> str:
    """Return a short-lived signed token encoding source_id (valid 1 hour, no server storage)."""
    payload = (
        base64.urlsafe_b64encode(
            json.dumps({"s": source_id, "e": int(time.time()) + 3600}).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    sig = hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{payload}.{sig}"


def _verify_install_state(state: str, secret_key: str) -> int | None:
    """Validate a state token; return source_id on success, None on failure."""
    try:
        payload, sig = state.rsplit(".", 1)
        expected = hmac.new(secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(sig, expected):
            return None
        padding = "=" * (4 - len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload + padding))
        if data["e"] < int(time.time()):
            return None
        return int(data["s"])
    except Exception:
        return None


class SourceResponse(BaseModel):
    id: int
    name: str
    provider: str
    app_id: str
    app_slug: str | None
    installation_id: str | None
    created_at: datetime


class GitHubCallbackBody(BaseModel):
    code: str
    installation_id: str | None = None
    name: str | None = None


class ManifestBody(BaseModel):
    name: str | None = None


class GitHubInstallBody(BaseModel):
    installation_id: str
    setup_action: str | None = None
    state: str | None = None


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
    body: ManifestBody,
    request: Request,
    user: User = Depends(get_current_user),
) -> Any:
    """Returns the manifest JSON for creating a GitHub App via the manifest flow."""
    # Respect X-Forwarded-Proto from a TLS-terminating proxy; otherwise use the
    # actual scheme of the incoming request so we never upgrade HTTP→HTTPS.
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    protocol = forwarded_proto.split(",")[0].strip() if forwarded_proto else request.url.scheme
    host = (
        request.headers.get("x-forwarded-host")
        or request.headers.get("host")
        or "localhost:8000"
    )
    base_url = f"{protocol}://{host}"
    app_name = body.name or f"hosty-{user.username}"

    manifest = {
        "name": app_name,
        "url": base_url,
        "hook_attributes": {
            "url": f"{base_url}/api/webhooks/github",
            "active": True,
        },
        "redirect_url": f"{base_url}/sources/github/callback",
        "setup_url": f"{base_url}/sources/github/install",
        "setup_on_update": True,
        "callback_urls": [f"{base_url}/sources/github/callback"],
        "request_oauth_on_install": False,
        "public": False,
        "default_permissions": {
            "contents": "read",
            "metadata": "read",
            "emails": "read",
            "administration": "read",
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

    # Prefer the installation_id from the redirect URL; if absent (GitHub doesn't
    # always include it when the installation step is separate), fetch it via the
    # GitHub API using the freshly-created app's JWT.
    installation_id = body.installation_id
    if not installation_id:
        now = int(time.time())
        try:
            app_jwt = pyjwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": str(data["id"])},
                data["pem"],
                algorithm="RS256",
            )
            async with httpx.AsyncClient() as gh:
                inst_resp = await gh.get(
                    "https://api.github.com/app/installations",
                    headers={
                        "Authorization": f"Bearer {app_jwt}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                )
            if inst_resp.status_code == 200:
                installations = inst_resp.json()
                if installations:
                    installation_id = str(installations[0]["id"])
        except Exception:
            pass  # leave installation_id as None; user can re-register or install manually

    source = GitSource(
        owner_id=user.id,
        name=app_name,
        provider="github",
        app_id=str(data["id"]),
        app_slug=data.get("slug") or app_name,
        installation_id=installation_id,
        client_id=data["client_id"],
        client_secret_encrypted=encrypt_secret(data["client_secret"], settings.secret_key),
        private_key_encrypted=encrypt_secret(data["pem"], settings.secret_key),
        webhook_secret_encrypted=encrypt_secret(data["webhook_secret"], settings.secret_key),
    )
    db.add(source)
    await db.commit()
    await db.refresh(source)
    return {"status": "ok", "source_id": source.id}


@router.post("/{source_id}/refresh-installation")
async def refresh_installation(
    source_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
) -> Any:
    """Re-fetch the installation ID from GitHub for a source that is missing one."""
    source = await db.get(GitSource, source_id)
    if not source or source.owner_id != user.id:
        raise NotFoundError("Source not found")

    settings = request.app.state.settings
    try:
        private_key = decrypt_secret(source.private_key_encrypted, settings.secret_key)
    except SecretDecryptionError:
        raise ConflictError(
            "Cannot decrypt this source's credentials — HOSTY_SECRET_KEY may have changed. "
            "Please delete and re-register the GitHub App."
        )
    now = int(time.time())
    app_jwt = pyjwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": str(source.app_id)},
        private_key,
        algorithm="RS256",
    )

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.github.com/app/installations",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github.v3+json",
            },
        )

    if resp.status_code != 200:
        raise ConflictError(f"Could not fetch installations from GitHub: {resp.text[:200]}")

    installations = resp.json()
    if not installations:
        raise ConflictError(
            "No installations found for this GitHub App. "
            "Install it at https://github.com/settings/apps."
        )

    source.installation_id = str(installations[0]["id"])
    await db.commit()
    await db.refresh(source)
    return {"status": "ok", "installation_id": source.installation_id}


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
    try:
        private_key = decrypt_secret(source.private_key_encrypted, settings.secret_key)
    except SecretDecryptionError:
        raise ConflictError(
            "Cannot decrypt this source's credentials — HOSTY_SECRET_KEY may have changed. "
            "Please delete and re-register the GitHub App."
        )

    now = int(time.time())
    app_jwt = pyjwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": str(source.app_id)},
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


@router.get("/{source_id}/install-url")
async def get_install_url(
    source_id: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
) -> Any:
    """Return a signed GitHub App installation URL (state token expires in 1 hour)."""
    source = await db.get(GitSource, source_id)
    if not source or source.owner_id != user.id:
        raise NotFoundError("Source not found")
    if not source.app_slug:
        raise ConflictError("Source has no app slug — please delete and re-register.")

    settings = request.app.state.settings
    state = _make_install_state(source.id, settings.secret_key)
    url = f"https://github.com/apps/{source.app_slug}/installations/new?state={state}"
    return {"url": url}


@router.post("/github/install")
async def github_install(
    body: GitHubInstallBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    request: Request = None,
) -> Any:
    """Handle GitHub's setup_url redirect after app installation or permission update."""
    if body.setup_action == "update":
        # Repository access change — no DB update needed
        return {"status": "ok"}

    settings = request.app.state.settings

    # Primary path: validate signed state token → direct source lookup
    if body.state:
        source_id = _verify_install_state(body.state, settings.secret_key)
        if source_id:
            source = await db.get(GitSource, source_id)
            if source and source.owner_id == user.id:
                source.installation_id = body.installation_id
                await db.commit()
                return {"status": "ok", "source_id": source.id}

    # Fallback: verify via GitHub API which app owns this installation
    result = await db.execute(
        select(GitSource).where(
            GitSource.owner_id == user.id,
            GitSource.installation_id.is_(None),
        )
    )
    sources = result.scalars().all()

    for source in sources:
        try:
            private_key = decrypt_secret(source.private_key_encrypted, settings.secret_key)
            now = int(time.time())
            app_jwt = pyjwt.encode(
                {"iat": now - 60, "exp": now + 540, "iss": str(source.app_id)},
                private_key,
                algorithm="RS256",
            )
            async with httpx.AsyncClient() as gh:
                resp = await gh.get(
                    f"https://api.github.com/app/installations/{body.installation_id}",
                    headers={
                        "Authorization": f"Bearer {app_jwt}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                )
            if resp.status_code == 200:
                inst = resp.json()
                if str(inst.get("app_id")) == str(source.app_id):
                    source.installation_id = body.installation_id
                    await db.commit()
                    return {"status": "ok", "source_id": source.id}
        except Exception:
            continue

    return {"status": "ok"}  # Could not match; user can use Refresh Installation
