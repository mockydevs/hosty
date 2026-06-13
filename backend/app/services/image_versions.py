"""Container image version catalog (v2/M4+, ADR-013).

Blueprints offer a version DROPDOWN whose options come from the image's
official registry (Docker Hub) instead of a hardcoded list, so new releases
show up without a code change. Results are cached with a TTL (registry calls
are slow and rate-limited) and DEGRADE GRACEFULLY: any network/parse failure
falls back to the caller-supplied default, so the create form always renders.

"Series" granularity is taken from the component count of the template's own
default ("16" -> 1 component, "11.4" -> 2), so the dropdown shows clean
series (postgres 17/16/15, mariadb 11.4/11.2/10.11) while the image still
pulls the latest patch within the series at run time. Pre-release tags
(anything with a non-numeric component like rc/beta) are excluded.
"""

from __future__ import annotations

import asyncio
import re
import time

import httpx
import structlog

log = structlog.get_logger("hosty.image_versions")

_TAGS_URL = "https://hub.docker.com/v2/repositories/{repo}/tags"
_PAGE_SIZE = 100
_CACHE_TTL = 6 * 3600.0  # a fresh, successful catalog is good for 6h
_FAILURE_TTL = 300.0  # retry a failed/empty fetch sooner (5 min)
_MAX_SERIES = 12
_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")  # pure numeric tags only

# normalized repo -> (monotonic expiry, series newest-first)
_cache: dict[str, tuple[float, list[str]]] = {}
_locks: dict[str, asyncio.Lock] = {}


def normalize_repo(repo: str) -> str:
    """Docker Hub API namespace form: `postgres` -> `library/postgres`,
    `docker.io/library/mongo:7` -> `library/mongo`, `louislam/uptime-kuma`
    unchanged. Strips any registry prefix, tag, or digest."""
    repo = repo.strip().removeprefix("docker.io/")
    repo = repo.split("@", 1)[0].split(":", 1)[0]
    return repo if "/" in repo else f"library/{repo}"


def _series_of(tag: str, components: int) -> str | None:
    if not _VERSION_RE.fullmatch(tag):
        return None
    parts = tag.split(".")
    if len(parts) < components:
        return None  # too coarse to express this series (e.g. "11" for an 11.4 series)
    return ".".join(parts[:components])


def reduce_to_series(tags: list[str], *, components: int, limit: int = _MAX_SERIES) -> list[str]:
    """Pure: numeric tags -> distinct series at `components` granularity,
    newest first, capped at `limit`."""
    seen: dict[str, tuple[int, ...]] = {}
    for tag in tags:
        series = _series_of(tag, components)
        if series is not None and series not in seen:
            seen[series] = tuple(int(p) for p in series.split("."))
    return sorted(seen, key=lambda s: seen[s], reverse=True)[:limit]


def _components_of(default: str) -> int:
    return max(1, len(default.split(".")))


async def _fetch_tags(repo: str, *, http: httpx.AsyncClient | None = None) -> list[str]:
    async def _go(client: httpx.AsyncClient) -> list[str]:
        resp = await client.get(
            _TAGS_URL.format(repo=repo),
            params={"page_size": _PAGE_SIZE, "ordering": "last_updated"},
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        return [
            r["name"] for r in results if isinstance(r, dict) and isinstance(r.get("name"), str)
        ]

    if http is not None:
        return await _go(http)
    async with httpx.AsyncClient(timeout=8.0) as client:
        return await _go(client)


async def available_series(
    repo: str, *, default: str, http: httpx.AsyncClient | None = None
) -> list[str]:
    """Series available for `repo`, newest first, cached. Never raises and
    always contains `default`: a fetch failure yields just `[default]` (cached
    briefly so it retries soon). `default` is moved to the front only when the
    registry did not already return it, so an online catalog leads with the
    genuine latest series."""
    norm = normalize_repo(repo)
    now = time.monotonic()
    cached = _cache.get(norm)
    if cached and cached[0] > now:
        return cached[1]

    lock = _locks.setdefault(norm, asyncio.Lock())
    async with lock:
        cached = _cache.get(norm)  # another waiter may have filled it
        if cached and cached[0] > time.monotonic():
            return cached[1]
        try:
            tags = await _fetch_tags(norm, http=http)
            series = reduce_to_series(tags, components=_components_of(default))
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("image_versions_fetch_failed", repo=norm, error=str(exc))
            series = []
        ok = bool(series)
        if default not in series:
            series = [default, *series]
        _cache[norm] = (time.monotonic() + (_CACHE_TTL if ok else _FAILURE_TTL), series)
        return series


def clear_cache() -> None:
    """Test seam: drop all cached catalogs."""
    _cache.clear()
