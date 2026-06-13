"""Caddy integration (ADR-001).

The panel owns desired state: `build_config` is a PURE function mapping site
specs to the complete Caddy JSON config (snapshot-testable), and `CaddyClient`
syncs it atomically through the Admin API. On a failed apply, the previous
config is restored automatically.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.domain.specs import StackSpec, derive_host_port

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
    # Behind the Cloudflare proxy: issue an internal origin certificate instead
    # of attempting ACME (HTTP-01 cannot complete through the proxy).
    internal_tls: bool = False
    # Phase 11d: owner account suspended — serve a 503 page instead of the site
    # (required for non-payment handling, not just a login block).
    suspended: bool = False


SUSPENDED_BODY = """\
<!doctype html>
<html lang="en">
<title>503 — Account suspended</title>
<style>body{font-family:system-ui,sans-serif;display:grid;place-items:center;min-height:90vh;color:#333}</style>
<body><div><h1>Account suspended</h1>
<p>This site is temporarily unavailable. Please contact the hosting administrator.</p></div></body>
</html>
"""


def _suspended_route(domain: str) -> dict[str, Any]:
    """Phase 11d: every request to a suspended owner's domain answers 503."""
    return {
        "match": [{"host": [domain]}],
        "handle": [
            {
                "handler": "static_response",
                "status_code": 503,
                "headers": {"Content-Type": ["text/html; charset=utf-8"]},
                "body": SUSPENDED_BODY,
            }
        ],
        "terminal": True,
    }


def _site_route(site: SiteSpec) -> dict[str, Any]:
    """One terminal route per domain: try_files rewrite → PHP-FPM → static files."""
    if site.suspended:
        return _suspended_route(site.domain)
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
class AppSpec:
    """Phase 12a: a containerized app vhost — domain proxied to the app's
    loopback-published container port. ORM-free, like SiteSpec."""

    domain: str
    upstream: str  # e.g. 127.0.0.1:20100
    internal_tls: bool = False  # behind the Cloudflare proxy (origin cert)
    suspended: bool = False


def _app_route(app: AppSpec) -> dict[str, Any]:
    """One terminal route per app domain: plain reverse proxy to the container."""
    if app.suspended:
        return _suspended_route(app.domain)
    return {
        "match": [{"host": [app.domain]}],
        "handle": [
            {
                "handler": "reverse_proxy",
                "upstreams": [{"dial": app.upstream}],
            }
        ],
        "terminal": True,
    }


@dataclass(frozen=True)
class StackRoute:
    """v2 (ADR-013): one stack endpoint — domain proxied to a service's
    loopback-published host port. Suspension renders the 503 page (the
    planner has already scaled the stack to zero)."""

    domain: str
    upstream: str  # 127.0.0.1:<host_port>
    internal_tls: bool = False  # behind the Cloudflare proxy (origin cert)
    suspended: bool = False


def routes_for_stack(spec: StackSpec, *, suspended: bool) -> list[StackRoute]:
    """Map a domain StackSpec's endpoints to Caddy routes. Endpoint cross-
    references (service exists, publishes a port) are guaranteed by StackSpec
    construction."""
    services_by_name = {service.name: service for service in spec.services}
    return [
        StackRoute(
            domain=endpoint.domain,
            upstream=f"{spec.loopback_ip}:{derive_host_port(services_by_name[endpoint.service].internal_port)}",
            internal_tls=endpoint.behind_cloudflare,
            suspended=suspended,
        )
        for endpoint in spec.endpoints
    ]


def _stack_route(route: StackRoute) -> dict[str, Any]:
    if route.suspended:
        return _suspended_route(route.domain)
    return {
        "match": [{"host": [route.domain]}],
        "handle": [
            {
                "handler": "reverse_proxy",
                "upstreams": [{"dial": route.upstream}],
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


@dataclass(frozen=True)
class PanelSpec:
    """Production: Caddy fronts the panel itself (HTTPS, optional IP allowlist)."""

    domain: str
    upstream: str  # e.g. 127.0.0.1:8800
    allowed_ips: tuple[str, ...] = ()


def _panel_route(spec: PanelSpec) -> dict[str, Any]:
    match: dict[str, Any] = {"host": [spec.domain]}
    if spec.allowed_ips:
        match["remote_ip"] = {"ranges": list(spec.allowed_ips)}
    return {
        "match": [match],
        "handle": [
            {
                "handler": "reverse_proxy",
                "upstreams": [{"dial": spec.upstream}],
            }
        ],
        "terminal": True,
    }


def build_config(
    sites: Sequence[SiteSpec],
    *,
    apps: Sequence[AppSpec] = (),
    stacks: Sequence[StackRoute] = (),
    adminer: AdminerSpec | None = None,
    tls_internal: bool = False,
    panel: PanelSpec | None = None,
    access_log_path: str | None = None,
) -> dict[str, Any]:
    """Full desired-state Caddy config. Deterministic: sites sorted by domain.

    `tls_internal` issues certificates from Caddy's internal CA instead of
    ACME — for dev VMs, where domains aren't publicly resolvable and Let's
    Encrypt would retry forever. Never enable it for production sites.

    `access_log_path` enables a JSON access log for the sites server — the
    source for per-vhost bandwidth metering (Phase 11d).
    """
    ordered = sorted(sites, key=lambda s: s.domain)
    ordered_apps = sorted(apps, key=lambda a: a.domain)
    ordered_stacks = sorted(stacks, key=lambda r: r.domain)
    servers: dict[str, Any] = {
        SERVER_NAME: {
            "listen": [":80", ":443"],
            "routes": ([_panel_route(panel)] if panel else [])
            + [_site_route(s) for s in ordered]
            + [_app_route(a) for a in ordered_apps]
            + [_stack_route(r) for r in ordered_stacks],
        }
    }
    if access_log_path:
        servers[SERVER_NAME]["logs"] = {"default_logger_name": "hosty_access"}
    if adminer is not None:
        servers["hosty_adminer"] = _adminer_server(adminer)
    config: dict[str, Any] = {
        "admin": {"listen": "127.0.0.1:2019"},
        "apps": {"http": {"servers": servers}},
    }
    if access_log_path:
        config["logging"] = {
            "logs": {
                "hosty_access": {
                    "writer": {"output": "file", "filename": access_log_path},
                    "encoder": {"format": "json"},
                    "include": ["http.log.access.hosty_access"],
                }
            }
        }
    if tls_internal and (ordered or ordered_apps or ordered_stacks):
        config["apps"]["tls"] = {
            "automation": {
                "policies": [
                    {
                        "subjects": [s.domain for s in ordered]
                        + [a.domain for a in ordered_apps]
                        + [r.domain for r in ordered_stacks],
                        "issuers": [{"module": "internal"}],
                    }
                ]
            }
        }
    else:
        # Sites behind the Cloudflare proxy get internal origin certificates
        # (Cloudflare terminates public TLS at the edge; SSL mode "Full").
        proxied = (
            [s.domain for s in ordered if s.internal_tls]
            + [a.domain for a in ordered_apps if a.internal_tls]
            + [r.domain for r in ordered_stacks if r.internal_tls]
        )
        if proxied:
            config["apps"]["tls"] = {
                "automation": {
                    "policies": [
                        {
                            "subjects": proxied,
                            "issuers": [{"module": "internal"}],
                        }
                    ]
                }
            }
    return config


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
