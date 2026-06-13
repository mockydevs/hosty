"""Stack image locking and stable-track updates.

Templates and user input declare source refs (`docker.io/library/postgres:16`).
At create/update time Hosty resolves that source to an immutable manifest
digest and stores it in `stack_services.image_digest`. The reconciler runs
the digest, not the mutable tag. Stable-track auto-updates only move that
stored digest; they never edit template source files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import utcnow
from app.db.models import Stack, StackService
from app.domain.specs import StackSpec
from app.domain.validate import validate_image_ref

log = structlog.get_logger("hosty.stack_images")

ACCEPT_MANIFESTS = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)
FLOATING_TAGS = {"latest", "stable", "edge", "main", "master", "nightly", "dev"}
UNSTABLE_MARKERS = ("alpha", "beta", "rc", "snapshot", "canary", "test")


class ImageResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageRef:
    source: str
    registry: str
    repository: str
    tag: str | None
    digest: str | None


def parse_image_ref(image: str) -> ImageRef:
    normalized = validate_image_ref(image)
    source, digest = (
        normalized.split("@sha256:", 1) if "@sha256:" in normalized else (normalized, None)
    )
    first, has_slash, rest = source.partition("/")
    if not has_slash:
        raise ImageResolutionError(f"Image is not fully qualified: {image!r}")
    repo_tag = rest
    last_slash = repo_tag.rfind("/")
    tag_colon = repo_tag.rfind(":")
    if tag_colon > last_slash:
        repository = repo_tag[:tag_colon]
        tag = repo_tag[tag_colon + 1 :]
    else:
        repository = repo_tag
        tag = None
    return ImageRef(
        source=source,
        registry=first,
        repository=repository,
        tag=tag,
        digest=digest,
    )


def pinned_ref(source: str, digest: str) -> str:
    digest = digest.removeprefix("sha256:")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ImageResolutionError(f"Invalid image digest: {digest!r}")
    return f"{parse_image_ref(source).source}@sha256:{digest}"


def is_stable_track(image: str) -> bool:
    ref = parse_image_ref(image)
    if ref.digest or not ref.tag:
        return False
    tag = ref.tag.lower()
    if tag in FLOATING_TAGS:
        return False
    return not any(marker in tag for marker in UNSTABLE_MARKERS)


async def _docker_hub_token(repository: str, client: httpx.AsyncClient) -> str:
    response = await client.get(
        "https://auth.docker.io/token",
        params={"service": "registry.docker.io", "scope": f"repository:{repository}:pull"},
    )
    response.raise_for_status()
    data = response.json()
    token = data.get("token")
    if not isinstance(token, str) or not token:
        raise ImageResolutionError("Docker Hub token response did not include a token")
    return token


async def _resolve_docker_hub(ref: ImageRef, client: httpx.AsyncClient) -> str:
    repository = ref.repository
    token = await _docker_hub_token(repository, client)
    response = await client.get(
        f"https://registry-1.docker.io/v2/{repository}/manifests/{ref.tag}",
        headers={"Authorization": f"Bearer {token}", "Accept": ACCEPT_MANIFESTS},
    )
    response.raise_for_status()
    digest = response.headers.get("Docker-Content-Digest", "")
    if not digest.startswith("sha256:"):
        raise ImageResolutionError(f"Registry did not return a digest for {ref.source}")
    return pinned_ref(ref.source, digest)


async def resolve_digest_ref(image: str, *, http: httpx.AsyncClient | None = None) -> str | None:
    """Resolve `image` to `source@sha256:digest`.

    A pre-pinned input is returned unchanged. Untagged images are treated as
    intentionally floating and are not auto-locked.
    """
    ref = parse_image_ref(image)
    if ref.digest:
        return f"{ref.source}@sha256:{ref.digest}"
    if not ref.tag:
        return None
    if ref.registry != "docker.io":
        # Public non-Docker-Hub registries can be added here with bearer
        # challenge handling; don't guess auth behavior for private registries.
        return None
    if http is not None:
        return await _resolve_docker_hub(ref, http)
    async with httpx.AsyncClient(timeout=12.0) as client:
        return await _resolve_docker_hub(ref, client)


async def lock_digest(image: str) -> str | None:
    try:
        return await resolve_digest_ref(image)
    except (httpx.HTTPError, ImageResolutionError) as exc:
        log.warning("stack_image_lock_failed", image=image, error=str(exc))
        return None


async def resolve_stack_image_locks(stack: StackSpec) -> dict[str, str | None]:
    """service name -> digest lock for non-build services."""
    locks: dict[str, str | None] = {}
    for service in stack.services:
        if service.build_repo:
            locks[service.name] = None
        else:
            locks[service.name] = await lock_digest(service.image)
    return locks


async def refresh_stable_image_locks(db: AsyncSession) -> int:
    """Refresh stored locks for stable image tracks. Returns changed services.

    Existing stacks move by updating `image_digest` and bumping generation; the
    reconciler then restarts only the affected units because their spec hash
    changes.
    """
    rows = (
        await db.execute(
            select(Stack, StackService)
            .join(StackService, StackService.stack_id == Stack.id)
            .where(
                Stack.status.in_(("converging", "ready", "degraded", "suspended", "error")),
                StackService.build_repo.is_(None),
            )
        )
    ).all()
    changed = 0
    for stack, service in rows:
        try:
            if not is_stable_track(service.image):
                continue
        except ImageResolutionError:
            continue
        latest = await lock_digest(service.image)
        if latest is None or latest == service.image_digest:
            continue
        service.image_digest = latest
        stack.generation += 1
        if stack.status in ("ready", "degraded", "error"):
            stack.status = "converging"
        stack.error_message = None
        stack.updated_at = utcnow()
        changed += 1
        log.info(
            "stack_image_lock_refreshed",
            stack=stack.name,
            service=service.name,
            image=service.image,
            digest=latest,
        )
    if changed:
        await db.commit()
    return changed
