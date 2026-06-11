"""Caddy service: pure config generation (snapshot) + apply/rollback client."""

from __future__ import annotations

import json

import httpx
import pytest

from app.services.caddy import CaddyClient, CaddyError, SiteSpec, build_config

DOC_ROOT = "/var/www/example.com/public_html"
SITE = SiteSpec(
    domain="example.com",
    doc_root=DOC_ROOT,
    php_socket="/run/php/site-example-com-abc123-php8.3.sock",
)
DIAL = f"unix/{SITE.php_socket}"


def test_build_config_snapshot():
    """Model in → exact Caddy JSON out. Update deliberately, never casually."""
    expected = {
        "admin": {"listen": "127.0.0.1:2019"},
        "apps": {
            "http": {
                "servers": {
                    "hosty": {
                        "listen": [":80", ":443"],
                        "routes": [
                            {
                                "match": [{"host": ["example.com"]}],
                                "handle": [
                                    {
                                        "handler": "subroute",
                                        "routes": [
                                            {
                                                "match": [
                                                    {
                                                        "file": {
                                                            "root": DOC_ROOT,
                                                            "try_files": [
                                                                "{http.request.uri.path}",
                                                                "{http.request.uri.path}/index.php",
                                                                "/index.php",
                                                            ],
                                                            "split_path": [".php"],
                                                        }
                                                    }
                                                ],
                                                "handle": [
                                                    {
                                                        "handler": "rewrite",
                                                        "uri": "{http.matchers.file.relative}",
                                                    }
                                                ],
                                            },
                                            {
                                                "match": [{"path": ["*.php"]}],
                                                "handle": [
                                                    {
                                                        "handler": "reverse_proxy",
                                                        "transport": {
                                                            "protocol": "fastcgi",
                                                            "root": DOC_ROOT,
                                                            "split_path": [".php"],
                                                        },
                                                        "upstreams": [{"dial": DIAL}],
                                                    }
                                                ],
                                            },
                                            {
                                                "handle": [
                                                    {
                                                        "handler": "file_server",
                                                        "root": DOC_ROOT,
                                                    }
                                                ]
                                            },
                                        ],
                                    }
                                ],
                                "terminal": True,
                            }
                        ],
                    }
                }
            }
        },
    }
    assert build_config([SITE]) == expected


def test_build_config_tls_internal_lists_all_domains():
    a = SiteSpec(domain="a.test", doc_root="/var/www/a.test/public", php_socket="/run/a.sock")
    z = SiteSpec(domain="z.test", doc_root="/var/www/z.test/public", php_socket="/run/z.sock")
    cfg = build_config([z, a], tls_internal=True)
    assert cfg["apps"]["tls"]["automation"]["policies"] == [
        {"subjects": ["a.test", "z.test"], "issuers": [{"module": "internal"}]}
    ]
    # Default stays ACME (no tls app at all) and empty configs add no policy.
    assert "tls" not in build_config([z, a])["apps"]
    assert "tls" not in build_config([], tls_internal=True)["apps"]


def test_build_config_empty():
    cfg = build_config([])
    assert cfg["apps"]["http"]["servers"]["hosty"]["routes"] == []


def test_build_config_deterministic_order():
    a = SiteSpec("a.com", "/var/www/a.com/public_html", "/run/php/a.sock")
    z = SiteSpec("z.com", "/var/www/z.com/public_html", "/run/php/z.sock")
    cfg = build_config([z, a])
    hosts = [r["match"][0]["host"][0] for r in cfg["apps"]["http"]["servers"]["hosty"]["routes"]]
    assert hosts == ["a.com", "z.com"]
    assert json.dumps(build_config([z, a])) == json.dumps(build_config([a, z]))


def _client(handler) -> CaddyClient:
    transport = httpx.MockTransport(handler)
    return CaddyClient(
        "http://caddy.test", client=httpx.AsyncClient(transport=transport, timeout=5)
    )


async def test_apply_success():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"old": True})
        return httpx.Response(200)

    await _client(handler).apply({"new": True})
    assert calls == [("GET", "/config/"), ("POST", "/load")]


async def test_apply_failure_restores_previous_config():
    loads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"old": True})
        body = json.loads(request.content)
        loads.append(body)
        # Reject the new config; accept the restore of the old one.
        return httpx.Response(400, text="invalid") if body.get("new") else httpx.Response(200)

    with pytest.raises(CaddyError):
        await _client(handler).apply({"new": True})
    assert loads == [{"new": True}, {"old": True}]


async def test_apply_unreachable_raises_caddy_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(CaddyError, match="unreachable"):
        await _client(handler).apply({"new": True})


def test_build_config_with_adminer_snapshot():
    from app.services.caddy import AdminerSpec

    spec = AdminerSpec(
        listen_addr="127.0.0.1:8081",
        root="/var/lib/hosty/adminer",
        php_socket="/run/php/hosty-adminer.sock",
    )
    cfg = build_config([], adminer=spec)
    assert cfg["apps"]["http"]["servers"]["hosty_adminer"] == {
        "listen": ["127.0.0.1:8081"],
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
                                            "root": "/var/lib/hosty/adminer",
                                            "split_path": [".php"],
                                        },
                                        "upstreams": [{"dial": "unix//run/php/hosty-adminer.sock"}],
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
    # The internal listener never appears without the spec.
    assert "hosty_adminer" not in build_config([])["apps"]["http"]["servers"]


def test_adminer_pool_render_snapshot():
    from app.core.config import Settings
    from app.services.adminer import render_adminer_pool

    pool = render_adminer_pool(Settings(_env_file=None))
    assert "[hosty-adminer]" in pool
    assert "user = www-data" in pool
    assert "listen = /run/php/hosty-adminer.sock" in pool
    assert "open_basedir] = /var/lib/hosty/adminer:/tmp" in pool
