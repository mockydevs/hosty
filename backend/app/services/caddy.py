"""Caddy integration (ADR-001).

The panel owns desired state: `build_config` is a PURE function mapping site
specs to the complete Caddy JSON config (snapshot-testable), and `CaddyClient`
syncs it atomically through the Admin API. On a failed apply, the previous
config is restored automatically.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

log = structlog.get_logger("hosty.caddy")

SERVER_NAME = "hosty"


class CaddyError(RuntimeError):
    """The Caddy Admin API rejected a request or is unreachable."""


@dataclass(frozen=True)
class SiteSpec:
    """The slice of a Site the web server cares about. Keep this ORM-free."""

    domain: str
    doc_root: str
    php_socket: str


def _site_route(site: SiteSpec) -> dict[str, Any]:
    """One terminal route per domain: try_files rewrite → PHP-FPM → static files."""
    return {
        "match": [{"host": [site.domain]}],
        "handle": [
            {
                "handler": "subroute",
                "routes": [
                    {
                        "match": [
                            {
                                "file": {
                                    "root": site.doc_root,
                                    "try_files": [
                                        "{http.request.uri.path}",
                                        "{http.request.uri.path}/index.php",
                                        "/index.php",
                                    ],
                                    "split_path": [".php"],
                                }
                            }
                        ],
                        "handle": [{"handler": "rewrite", "uri": "{http.matchers.file.relative}"}],
                    },
                    {
                        "match": [{"path": ["*.php"]}],
                        "handle": [
                            {
                                "handler": "reverse_proxy",
                                "transport": {
                                    "protocol": "fastcgi",
                                    "root": site.doc_root,
                                    "split_path": [".php"],
                                },
                                "upstreams": [{"dial": f"unix/{site.php_socket}"}],
                            }
                        ],
                    },
                    {"handle": [{"handler": "file_server", "root": site.doc_root}]},
                ],
            }
        ],
        "terminal": True,
    }


@dataclass(frozen=True)
class AdminerSpec:
    """Internal-only Adminer server: reached solely via the panel's proxy."""

    listen_addr: str  # e.g. 127.0.0.1:8081
    root: str  # directory containing adminer.php
    php_socket: str


def _adminer_server(spec: AdminerSpec) -> dict[str, Any]:
    return {
        "listen": [spec.listen_addr],
        "routes": [
            {
                "handle": [
                    {
                        "handler": "subroute",
                        "routes": [
                            {
                                "handle": [
                                    {"handler": "rewrite", "uri": "/adminer.php"},
                                    {
                                        "handler": "reverse_proxy",
                                        "transport": {
                                            "protocol": "fastcgi",
                                            "root": spec.root,
                                            "split_path": [".php"],
                                        },
                                        "upstreams": [{"dial": f"unix/{spec.php_socket}"}],
                                    },
                                ]
                            }
                        ],
                    }
                ],
                "terminal": True,
            }
        ],
    }


def build_config(
    sites: Sequence[SiteSpec], *, adminer: AdminerSpec | None = None
) -> dict[str, Any]:
    """Full desired-state Caddy config. Deterministic: sites sorted by domain."""
    ordered = sorted(sites, key=lambda s: s.domain)
    servers: dict[str, Any] = {
        SERVER_NAME: {
            "listen": [":80", ":443"],
            "routes": [_site_route(s) for s in ordered],
        }
    }
    if adminer is not None:
        servers["hosty_adminer"] = _adminer_server(adminer)
    return {
        "admin": {"listen": "127.0.0.1:2019"},
        "apps": {"http": {"servers": servers}},
    }


class CaddyClient:
    """Thin async wrapper over the Caddy Admin API."""

    def __init__(self, base_url: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            if self._client is not None:
                return await self._client.request(method, self._base_url + path, **kwargs)
            async with httpx.AsyncClient(timeout=10.0) as client:
                return await client.request(method, self._base_url + path, **kwargs)
        except httpx.HTTPError as exc:
            raise CaddyError(f"Caddy Admin API unreachable: {exc}") from exc

    async def get_config(self) -> dict[str, Any] | None:
        resp = await self._request("GET", "/config/")
        if resp.status_code != 200:
            raise CaddyError(f"GET /config/ returned {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    async def load(self, config: dict[str, Any]) -> None:
        """POST /load validates and applies atomically; non-2xx keeps the old config."""
        resp = await self._request("POST", "/load", json=config)
        if resp.status_code // 100 != 2:
            raise CaddyError(f"Caddy rejected config ({resp.status_code}): {resp.text[:500]}")

    async def apply(self, config: dict[str, Any]) -> None:
        """Apply `config`; if anything goes wrong, restore the previous config."""
        previous = await self.get_config()
        try:
            await self.load(config)
        except CaddyError:
            if previous is not None:
                try:
                    await self.load(previous)
                    log.warning("caddy_apply_failed_previous_restored")
                except CaddyError:
                    log.error("caddy_restore_failed")
            raise
