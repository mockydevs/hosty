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
    # active | no_certificate | dns_unresolved
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


async def probe(domain: str, *, host: str = "127.0.0.1", port: int = 443) -> CertStatus:
    """Handshake against the local Caddy with SNI=domain; report cert status."""
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
    context.check_hostname = True
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
        ssl_object = writer.get_extra_info("ssl_object")
        cert = ssl_object.getpeercert() if ssl_object else None
        return parse_peer_cert(domain, cert or {})
    finally:
        writer.close()
