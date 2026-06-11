"""PowerDNS integration (Phase 7).

Zones and records live in PowerDNS — the single source of truth; the panel
keeps no DNS tables. Validation/normalization helpers are pure functions
(unit-tested without a server); `PowerDNSClient` is a thin async wrapper over
the PowerDNS REST API.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from app.core.errors import AppError, ConflictError, NotFoundError

log = structlog.get_logger("hosty.dns")

RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA")

MIN_TTL = 60
MAX_TTL = 7 * 86400
MAX_RECORDS_PER_RRSET = 16
MAX_TXT_LENGTH = 255


class DNSError(AppError):
    """PowerDNS rejected a request or is unreachable."""

    status_code = 502
    code = "dns_error"


class DNSValidationError(AppError):
    status_code = 422
    code = "dns_validation_error"


class ZoneNotFoundError(NotFoundError):
    pass


# --- names ---------------------------------------------------------------------

_HOST_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
# Record names additionally allow service labels (_dmarc, _sip) and a leftmost wildcard.
_RECORD_LABEL = re.compile(r"^_?[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def canonical(name: str) -> str:
    """Lowercased, with exactly one trailing dot (PowerDNS canonical form)."""
    n = name.strip().lower().rstrip(".")
    return f"{n}."


def validate_zone_name(raw: str) -> str:
    """A zone must be at least two valid hostname labels. Returns canonical form."""
    name = raw.strip().lower().rstrip(".")
    if not name or len(name) > 253:
        raise DNSValidationError(f"Invalid zone name: {raw!r}")
    labels = name.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.fullmatch(label) for label in labels):
        raise DNSValidationError(f"Invalid zone name: {raw!r}")
    return f"{name}."


def validate_hostname(raw: str) -> str:
    """A target hostname (CNAME/NS/MX/SRV content). Returns canonical form."""
    name = raw.strip().lower().rstrip(".")
    if not name or len(name) > 253:
        raise DNSValidationError(f"Invalid hostname: {raw!r}")
    if not all(_HOST_LABEL.fullmatch(label) for label in name.split(".")):
        raise DNSValidationError(f"Invalid hostname: {raw!r}")
    return f"{name}."


def normalize_record_name(name: str, zone: str) -> str:
    """Resolve a record name ('@', relative, or absolute) to an FQDN inside `zone`."""
    zone_c = canonical(zone)
    n = (name or "@").strip().lower()
    if n in ("@", ""):
        return zone_c
    if n.endswith("."):
        if n != zone_c and not n.endswith(f".{zone_c}"):
            raise DNSValidationError(f"{n} is outside zone {zone_c}")
        fqdn = n
        relative = n.removesuffix(zone_c).rstrip(".")
    else:
        fqdn = f"{n}.{zone_c}"
        relative = n
    if relative:
        labels = relative.split(".")
        for i, label in enumerate(labels):
            if label == "*" and i == 0:
                continue  # wildcard allowed only as the leftmost label
            if not _RECORD_LABEL.fullmatch(label):
                raise DNSValidationError(f"Invalid record name: {name!r}")
    if len(fqdn) > 254:
        raise DNSValidationError(f"Record name too long: {name!r}")
    return fqdn


# --- record content ---------------------------------------------------------------

_MX_RE = re.compile(r"^(\d{1,5})\s+(\S+)$")
_SRV_RE = re.compile(r"^(\d{1,5})\s+(\d{1,5})\s+(\d{1,5})\s+(\S+)$")
_CAA_RE = re.compile(r'^(\d{1,3})\s+(issue|issuewild|iodef)\s+"?([^"]*)"?$')


def _u16(value: str, what: str) -> int:
    n = int(value)
    if n > 65535:
        raise DNSValidationError(f"{what} must be 0-65535")
    return n


def validate_content(rtype: str, content: str) -> str:
    """Validate and normalize one record value for `rtype`."""
    c = content.strip()
    if not c:
        raise DNSValidationError("Record value must not be empty")

    if rtype == "A":
        try:
            return str(ipaddress.IPv4Address(c))
        except ValueError as exc:
            raise DNSValidationError(f"Invalid IPv4 address: {c!r}") from exc
    if rtype == "AAAA":
        try:
            return str(ipaddress.IPv6Address(c))
        except ValueError as exc:
            raise DNSValidationError(f"Invalid IPv6 address: {c!r}") from exc
    if rtype in ("CNAME", "NS"):
        return validate_hostname(c)
    if rtype == "MX":
        m = _MX_RE.fullmatch(c)
        if not m:
            raise DNSValidationError("MX value must be '<priority> <mailserver>'")
        return f"{_u16(m[1], 'MX priority')} {validate_hostname(m[2])}"
    if rtype == "SRV":
        m = _SRV_RE.fullmatch(c)
        if not m:
            raise DNSValidationError("SRV value must be '<priority> <weight> <port> <target>'")
        prio, weight, port = (
            _u16(m[i], f) for i, f in ((1, "priority"), (2, "weight"), (3, "port"))
        )
        return f"{prio} {weight} {port} {validate_hostname(m[4])}"
    if rtype == "TXT":
        inner = c[1:-1] if len(c) >= 2 and c.startswith('"') and c.endswith('"') else c
        if len(inner) > MAX_TXT_LENGTH:
            raise DNSValidationError(f"TXT value exceeds {MAX_TXT_LENGTH} characters")
        escaped = inner.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if rtype == "CAA":
        m = _CAA_RE.fullmatch(c)
        if not m:
            raise DNSValidationError(
                "CAA value must be '<flags> issue|issuewild|iodef \"<value>\"'"
            )
        flags = int(m[1])
        if flags > 255:
            raise DNSValidationError("CAA flags must be 0-255")
        return f'{flags} {m[2]} "{m[3]}"'
    raise DNSValidationError(f"Unsupported record type: {rtype!r}")


# --- rrsets ---------------------------------------------------------------------


@dataclass(frozen=True)
class RRSet:
    name: str  # FQDN with trailing dot
    rtype: str
    ttl: int
    records: tuple[str, ...]


def make_rrset(zone: str, name: str, rtype: str, ttl: int, contents: list[str]) -> RRSet:
    rtype = rtype.strip().upper()
    if rtype not in RECORD_TYPES:
        raise DNSValidationError(
            f"Unsupported record type {rtype!r} (supported: {', '.join(RECORD_TYPES)})"
        )
    fqdn = normalize_record_name(name, zone)
    if rtype == "CNAME" and fqdn == canonical(zone):
        raise DNSValidationError("A CNAME record cannot be placed at the zone apex")
    if not MIN_TTL <= ttl <= MAX_TTL:
        raise DNSValidationError(f"TTL must be between {MIN_TTL} and {MAX_TTL} seconds")
    if not contents:
        raise DNSValidationError("At least one record value is required")
    if len(contents) > MAX_RECORDS_PER_RRSET:
        raise DNSValidationError(f"At most {MAX_RECORDS_PER_RRSET} values per record set")
    validated = tuple(validate_content(rtype, c) for c in contents)
    if len(set(validated)) != len(validated):
        raise DNSValidationError("Duplicate record values")
    if rtype == "CNAME" and len(validated) > 1:
        raise DNSValidationError("A CNAME record set can hold only one value")
    return RRSet(name=fqdn, rtype=rtype, ttl=ttl, records=validated)


def replace_patch(rrset: RRSet) -> dict[str, Any]:
    return {
        "name": rrset.name,
        "type": rrset.rtype,
        "ttl": rrset.ttl,
        "changetype": "REPLACE",
        "records": [{"content": c, "disabled": False} for c in rrset.records],
    }


def delete_patch(zone: str, name: str, rtype: str) -> dict[str, Any]:
    rtype = rtype.strip().upper()
    if rtype not in RECORD_TYPES:
        raise DNSValidationError(f"Unsupported record type: {rtype!r}")
    return {
        "name": normalize_record_name(name, zone),
        "type": rtype,
        "changetype": "DELETE",
        "records": [],
    }


def default_nameservers(zone: str, configured: list[str]) -> list[str]:
    """Configured nameservers, or ns1/ns2 under the zone itself (self-hosted DNS)."""
    if configured:
        return [canonical(ns) for ns in configured]
    zone_c = canonical(zone)
    return [f"ns1.{zone_c}", f"ns2.{zone_c}"]


# --- PowerDNS REST client ---------------------------------------------------------


@dataclass(frozen=True)
class ZoneSummary:
    id: str
    name: str
    kind: str
    serial: int


class PowerDNSClient:
    """Thin async wrapper over the PowerDNS Authoritative Server API."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        server_id: str = "localhost",
        *,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = f"{api_url.rstrip('/')}/servers/{server_id}"
        self._headers = {"X-API-Key": api_key}
        self._http = http

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url = self._base + path
        try:
            if self._http is not None:
                return await self._http.request(method, url, headers=self._headers, **kwargs)
            async with httpx.AsyncClient(timeout=10.0) as client:
                return await client.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise DNSError(f"PowerDNS API unreachable: {exc.__class__.__name__}") from exc

    @staticmethod
    def _unexpected(resp: httpx.Response, doing: str) -> DNSError:
        return DNSError(f"PowerDNS {doing} failed ({resp.status_code}): {resp.text[:300]}")

    async def list_zones(self) -> list[ZoneSummary]:
        resp = await self._request("GET", "/zones")
        if resp.status_code != 200:
            raise self._unexpected(resp, "list zones")
        return [
            ZoneSummary(
                id=z["id"], name=z["name"], kind=z.get("kind", ""), serial=z.get("serial", 0)
            )
            for z in resp.json()
        ]

    async def get_zone(self, zone_id: str) -> dict[str, Any]:
        resp = await self._request("GET", f"/zones/{zone_id}")
        if resp.status_code == 404:
            raise ZoneNotFoundError(f"Zone not found: {zone_id}")
        if resp.status_code != 200:
            raise self._unexpected(resp, "get zone")
        return resp.json()

    async def create_zone(self, name: str, nameservers: list[str]) -> dict[str, Any]:
        payload = {"name": name, "kind": "Native", "nameservers": nameservers}
        resp = await self._request("POST", "/zones", json=payload)
        if resp.status_code == 409 or (resp.status_code == 422 and "exist" in resp.text.lower()):
            raise ConflictError(f"Zone {name} already exists")
        if resp.status_code not in (200, 201):
            raise self._unexpected(resp, "create zone")
        return resp.json()

    async def delete_zone(self, zone_id: str) -> None:
        resp = await self._request("DELETE", f"/zones/{zone_id}")
        if resp.status_code == 404:
            raise ZoneNotFoundError(f"Zone not found: {zone_id}")
        if resp.status_code not in (200, 204):
            raise self._unexpected(resp, "delete zone")

    async def patch_rrsets(self, zone_id: str, patches: list[dict[str, Any]]) -> None:
        resp = await self._request("PATCH", f"/zones/{zone_id}", json={"rrsets": patches})
        if resp.status_code == 404:
            raise ZoneNotFoundError(f"Zone not found: {zone_id}")
        if resp.status_code == 422:
            raise DNSValidationError(f"PowerDNS rejected the change: {resp.text[:300]}")
        if resp.status_code not in (200, 204):
            raise self._unexpected(resp, "update records")
