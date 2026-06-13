"""Image version catalog (v2/M4+): pure series reduction, registry fetch via
a mocked transport, caching, and graceful fallback to the template default."""

from __future__ import annotations

import httpx
import pytest

from app.services import image_versions as iv


@pytest.fixture(autouse=True)
def _clear_cache():
    iv.clear_cache()
    yield
    iv.clear_cache()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    "repo,expected",
    [
        ("postgres", "library/postgres"),
        ("docker.io/library/mongo:7.0", "library/mongo"),
        ("louislam/uptime-kuma", "louislam/uptime-kuma"),
        ("docker.io/filebrowser/filebrowser:v2@sha256:abc", "filebrowser/filebrowser"),
    ],
)
def test_normalize_repo(repo, expected):
    assert iv.normalize_repo(repo) == expected


def test_reduce_to_series_major_only():
    tags = ["17.2", "16.4", "16.6", "15.8", "latest", "16-bookworm", "13", "9.6.24"]
    # components=1 → distinct majors, newest first; non-numeric tags dropped.
    assert iv.reduce_to_series(tags, components=1) == ["17", "16", "15", "13", "9"]


def test_reduce_to_series_two_components():
    tags = ["11.4.3", "11.4.2", "11.2.0", "10.11.5", "11", "garbage"]
    # "11" is too coarse for a 2-component series and is skipped.
    assert iv.reduce_to_series(tags, components=2) == ["11.4", "11.2", "10.11"]


def test_reduce_to_series_caps_length():
    tags = [f"{n}.0" for n in range(40)]
    assert len(iv.reduce_to_series(tags, components=2, limit=5)) == 5


async def test_available_series_fetches_and_reduces():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "library/postgres/tags" in str(request.url)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"name": "17.2"},
                    {"name": "16.4"},
                    {"name": "latest"},
                    {"name": "15.8"},
                ]
            },
        )

    series = await iv.available_series("postgres", default="16", http=_client(handler))
    # Newest first, reduced to majors; the registry already includes "16" so
    # it is NOT moved to the front — the genuine latest leads.
    assert series == ["17", "16", "15"]


async def test_available_series_caches_after_first_fetch():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"results": [{"name": "16.4"}, {"name": "15.8"}]})

    client = _client(handler)
    first = await iv.available_series("postgres", default="16", http=client)
    second = await iv.available_series("postgres", default="16", http=client)
    assert first == second == ["16", "15"]
    assert calls["n"] == 1  # second call served from cache


async def test_available_series_falls_back_to_default_on_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    series = await iv.available_series("postgres", default="16", http=_client(handler))
    assert series == ["16"]  # never raises; default always present


async def test_available_series_prepends_missing_default():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [{"name": "17.2"}, {"name": "16.4"}]})

    # The configured default is older than anything the registry returned —
    # keep it selectable, but the latest still leads the list.
    series = await iv.available_series("postgres", default="14", http=_client(handler))
    assert series == ["14", "17", "16"]
