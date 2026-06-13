from __future__ import annotations

import httpx
import pytest

from app.services import stack_images


def test_parse_image_ref_normalizes_short_names():
    ref = stack_images.parse_image_ref("postgres:16")

    assert ref.source == "docker.io/library/postgres:16"
    assert ref.registry == "docker.io"
    assert ref.repository == "library/postgres"
    assert ref.tag == "16"
    assert ref.digest is None


def test_stable_track_detection():
    assert stack_images.is_stable_track("docker.io/library/postgres:16") is True
    assert stack_images.is_stable_track("docker.io/library/nginx:1.27-alpine") is True
    assert stack_images.is_stable_track("docker.io/library/rabbitmq:3-management") is True
    assert stack_images.is_stable_track("docker.io/library/postgres:latest") is False
    assert stack_images.is_stable_track("docker.io/library/app:1.0-rc1") is False
    assert (
        stack_images.is_stable_track("docker.io/library/postgres:16@sha256:" + "a" * 64)
        is False
    )


async def test_resolve_digest_ref_with_mocked_docker_hub():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "auth.docker.io":
            return httpx.Response(200, json={"token": "token"})
        assert request.url.path == "/v2/library/postgres/manifests/16"
        return httpx.Response(200, headers={"Docker-Content-Digest": "sha256:" + "b" * 64})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        locked = await stack_images.resolve_digest_ref("postgres:16", http=client)

    assert locked == "docker.io/library/postgres:16@sha256:" + "b" * 64


async def test_resolve_digest_ref_returns_none_for_untagged_and_non_docker_hub():
    assert await stack_images.resolve_digest_ref("docker.io/library/postgres") is None
    assert await stack_images.resolve_digest_ref("ghcr.io/example/app:v1") is None


def test_pinned_ref_rejects_bad_digest():
    with pytest.raises(stack_images.ImageResolutionError):
        stack_images.pinned_ref("docker.io/library/postgres:16", "short")
