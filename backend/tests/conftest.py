from __future__ import annotations

import hashlib

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app

TEST_USER = "admin"
TEST_PASSWORD = "correct-horse-battery"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        secret_key="test-secret-key-at-least-32-bytes-long!",
        database_url="sqlite+aiosqlite:///:memory:",
        create_tables_on_startup=True,
        cookie_secure=False,
        caddy_sync_on_startup=False,
        _env_file=None,
    )


@pytest.fixture(autouse=True)
def _offline_image_versions(monkeypatch):
    """Keep the version catalog off the network in tests. Real registry
    fetches return []  (so blueprints fall back to their template default);
    tests that inject an httpx transport via `http=` still exercise the real
    fetch path against their mock."""
    from app.services import image_versions

    real_fetch = image_versions._fetch_tags

    async def fetch(repo, *, http=None):
        if http is not None:
            return await real_fetch(repo, http=http)
        return []

    monkeypatch.setattr(image_versions, "_fetch_tags", fetch)
    image_versions.clear_cache()


@pytest.fixture(autouse=True)
def _offline_stack_image_locks(monkeypatch):
    """Resolve stack image locks deterministically without registry access."""
    from app.services import stack_images

    async def lock(image: str) -> str | None:
        ref = stack_images.parse_image_ref(image)
        if ref.digest:
            return f"{ref.source}@sha256:{ref.digest}"
        if not ref.tag:
            return None
        digest = hashlib.sha256(ref.source.encode()).hexdigest()
        return f"{ref.source}@sha256:{digest}"

    monkeypatch.setattr(stack_images, "lock_digest", lock)


@pytest_asyncio.fixture
async def app(settings):
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def setup_and_login(client: AsyncClient) -> str:
    """Create the admin user, log in, and return the access token."""
    resp = await client.post(
        "/api/auth/setup", json={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post(
        "/api/auth/login", json={"username": TEST_USER, "password": TEST_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


@pytest_asyncio.fixture
async def admin_client(client):
    token = await setup_and_login(client)
    client.headers["Authorization"] = f"Bearer {token}"
    return client
