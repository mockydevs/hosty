"""Per-domain certificate status.

Caddy obtains certificates automatically; the panel surfaces whether that
worked by (1) checking the domain resolves at all and (2) performing a local
TLS handshake with the domain as SNI. The "DNS not pointing here" failure mode
gets a clear, actionable message instead of a generic error.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class CertStatus:
    domain: str
    # active | origin_internal | no_certificate | dns_unresolved
    status: str
    issuer: str | None = None
    not_after: datetime | None = None
    detail: str | None = None


def parse_peer_cert(domain: str, cert: dict) -> CertStatus:
    """Pure: ssl.getpeercert() dict in → CertStatus out."""
    issuer = None
    for rdn in cert.get("issuer", ()):  # (((key, value),), ...)
        for key, value in rdn:
            if key == "organizationName":
                issuer = value
    not_after = None
    raw = cert.get("notAfter")
    if raw:
        # e.g. 'Jun 11 12:00:00 2026 GMT'
        not_after = datetime.strptime(raw, "%b %d %H:%M:%S %Y %Z")
    return CertStatus(domain=domain, status="active", issuer=issuer, not_after=not_after)


async def _resolves(domain: str) -> bool:
    loop = asyncio.get_running_loop()
    try:
        await loop.getaddrinfo(domain, 443, type=socket.SOCK_STREAM)
        return True
    except socket.gaierror:
        return False


async def probe(
    domain: str, *, host: str = "127.0.0.1", port: int = 443, verify: bool = True
) -> CertStatus:
    """Handshake against the local Caddy with SNI=domain; report cert status.

    `verify=False` is for sites behind the Cloudflare proxy: the origin serves
    Caddy's internal certificate (untrusted by design), so we only check that a
    TLS handshake completes and report it as `origin_internal`.
    """
    if not await _resolves(domain):
        return CertStatus(
            domain=domain,
            status="dns_unresolved",
            detail=(
                "The domain does not resolve yet. Point an A/AAAA record at this "
                "server; Caddy will obtain a certificate automatically once it does."
            ),
        )
    context = ssl.create_default_context()
    if verify:
        context.check_hostname = True
    else:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context, server_hostname=domain),
            timeout=10,
        )
    except (TimeoutError, asyncio.TimeoutError, OSError, ssl.SSLError) as exc:
        return CertStatus(
            domain=domain,
            status="no_certificate",
            detail=(
                "No valid certificate is being served for this domain yet "
                f"({exc.__class__.__name__}). If DNS only recently started pointing "
                "here, Caddy may still be obtaining one."
            ),
        )
    try:
        if not verify:
            # With verification off, getpeercert() returns nothing useful; the
            # successful handshake is the signal.
            return CertStatus(
                domain=domain,
                status="origin_internal",
                issuer="Caddy internal CA",
                detail=(
                    "The origin serves an internal certificate; visitors get "
                    "Cloudflare's edge certificate. Set the Cloudflare SSL mode "
                    'to "Full".'
                ),
            )
        ssl_object = writer.get_extra_info("ssl_object")
        cert = ssl_object.getpeercert() if ssl_object else None
        return parse_peer_cert(domain, cert or {})
    finally:
        writer.close()
