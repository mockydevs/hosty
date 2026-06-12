"""Cloudflare DNS push (Phase 7 extension).

One-click export of a PowerDNS zone to Cloudflare: identical records are
skipped, changed single-value records are updated in place, missing records
are created. The panel NEVER deletes Cloudflare records. The zone must already
exist in the Cloudflare account (adding a domain assigns nameservers there).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from app.core.errors import AppError, ConflictError

log = structlog.get_logger("hosty.cloudflare")

API_BASE = "https://api.cloudflare.com/client/v4"
PUSH_TYPES = ("A", "AAAA", "CNAME", "MX", "TXT", "NS", "SRV", "CAA")


class CloudflareError(AppError):
    status_code = 502
    code = "cloudflare_error"


class ZoneNotInCloudflareError(ConflictError):
    code = "cloudflare_zone_missing"


@dataclass
class PushResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


# --- pure mapping: panel rrsets -> Cloudflare payloads -----------------------------

_MX_RE = re.compile(r"^(\d+)\s+(\S+)$")
_SRV_RE = re.compile(r"^(\d+)\s+(\d+)\s+(\d+)\s+(\S+)$")
_CAA_RE = re.compile(r'^(\d+)\s+(\S+)\s+"?([^"]*)"?$')


def _strip_dot(name: str) -> str:
    return name.rstrip(".")


def _unquote_txt(content: str) -> str:
    c = content.strip()
    if len(c) >= 2 and c.startswith('"') and c.endswith('"'):
        c = c[1:-1]
    return c.replace('\\"', '"').replace("\\\\", "\\")


def _payload(base: dict[str, Any], rtype: str, cf_name: str, content: str) -> dict[str, Any]:
    if rtype in ("A", "AAAA"):
        return {**base, "content": content, "proxied": False}
    if rtype in ("CNAME", "NS"):
        return {**base, "content": _strip_dot(content), "proxied": False}
    if rtype == "TXT":
        return {**base, "content": _unquote_txt(content)}
    if rtype == "MX":
        m = _MX_RE.fullmatch(content.strip())
        if not m:
            raise ValueError(f"cannot parse MX value {content!r}")
        return {**base, "content": _strip_dot(m[2]), "priority": int(m[1])}
    if rtype == "CAA":
        m = _CAA_RE.fullmatch(content.strip())
        if not m:
            raise ValueError(f"cannot parse CAA value {content!r}")
        return {**base, "data": {"flags": int(m[1]), "tag": m[2], "value": m[3]}}
    if rtype == "SRV":
        labels = cf_name.split(".")
        if len(labels) < 3 or not labels[0].startswith("_") or not labels[1].startswith("_"):
            raise ValueError("SRV name must look like _service._proto.domain")
        m = _SRV_RE.fullmatch(content.strip())
        if not m:
            raise ValueError(f"cannot parse SRV value {content!r}")
        return {
            **base,
            "data": {
                "service": labels[0],
                "proto": labels[1],
                "name": ".".join(labels[2:]),
                "priority": int(m[1]),
                "weight": int(m[2]),
                "port": int(m[3]),
                "target": _strip_dot(m[4]),
            },
        }
    raise ValueError(f"unsupported type {rtype}")


def desired_records(
    zone: str, rrsets: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Map panel rrsets to Cloudflare record payloads. Pure and unit-tested.

    SOA is never pushed; apex NS is skipped because Cloudflare manages its own.
    Unmappable records are reported as errors, not fatal.
    """
    zone_c = zone if zone.endswith(".") else f"{zone}."
    out: list[dict[str, Any]] = []
    errors: list[str] = []
    for rrset in rrsets:
        rtype = rrset["type"]
        if rtype not in PUSH_TYPES:
            continue
        name = rrset["name"]
        if rtype == "NS" and name == zone_c:
            continue
        cf_name = _strip_dot(name)
        ttl = max(60, int(rrset.get("ttl") or 60))
        for content in rrset["records"]:
            base = {"type": rtype, "name": cf_name, "ttl": ttl}
            try:
                out.append(_payload(base, rtype, cf_name, content))
            except ValueError as exc:
                errors.append(f"{rtype} {cf_name}: {exc}")
    return out, errors


def _same(desired: dict[str, Any], existing: dict[str, Any]) -> bool:
    if desired["type"] != existing.get("type") or desired["name"] != existing.get("name"):
        return False
    if "data" in desired:
        ex = existing.get("data") or {}
        return all(str(ex.get(k)) == str(v) for k, v in desired["data"].items())
    if desired["type"] == "MX" and int(existing.get("priority", -1)) != int(
        desired.get("priority", -2)
    ):
        return False
    if desired["type"] == "TXT":
        return _unquote_txt(str(existing.get("content", ""))) == desired["content"]
    return existing.get("content") == desired.get("content")


# --- pure mapping: Cloudflare records -> panel rrsets (pull) -----------------------


def pulled_rrsets(
    zone: str, cf_records: list[dict[str, Any]], default_ttl: int
) -> tuple[list[tuple[str, str, int, list[str]]], list[str]]:
    """The inverse of desired_records: Cloudflare records -> (name, type, ttl,
    contents) tuples ready for dns.make_rrset. Pure and unit-tested.

    SOA is never pulled (PowerDNS owns it) and apex NS is skipped (those are
    Cloudflare's own nameservers). Cloudflare ttl=1 means "auto" -> default_ttl.
    Unmappable records are reported as errors, not fatal.
    """
    zone_b = _strip_dot(zone)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    errors: list[str] = []
    for r in cf_records:
        rtype = str(r.get("type", "")).upper()
        name = _strip_dot(str(r.get("name", "")))
        if rtype not in PUSH_TYPES:
            errors.append(f"{rtype or '?'} {name}: unsupported type")
            continue
        if rtype == "NS" and name == zone_b:
            continue
        try:
            content = _pull_content(rtype, r)
        except (KeyError, ValueError) as exc:
            errors.append(f"{rtype} {name}: {exc}")
            continue
        ttl = int(r.get("ttl") or 1)
        if ttl <= 1:
            ttl = default_ttl
        # Emit FQDNs (trailing dot): bare names would be read as RELATIVE by
        # normalize_record_name and get the zone appended twice.
        slot = grouped.setdefault((f"{name}.", rtype), {"ttl": ttl, "contents": []})
        if content not in slot["contents"]:
            slot["contents"].append(content)
    out = [
        (name, rtype, slot["ttl"], slot["contents"])
        for (name, rtype), slot in sorted(grouped.items())
    ]
    return out, errors


def _pull_content(rtype: str, r: dict[str, Any]) -> str:
    content = str(r.get("content") or "").strip()
    if rtype in ("A", "AAAA"):
        return content
    if rtype in ("CNAME", "NS"):
        return f"{_strip_dot(content)}."
    if rtype == "TXT":
        return content  # validate_content quotes/escapes it
    if rtype == "MX":
        prio = r.get("priority")
        if prio is None:
            raise ValueError("missing priority")
        return f"{int(prio)} {_strip_dot(content)}."
    if rtype == "SRV":
        d = r.get("data") or {}
        try:
            return (
                f"{int(d['priority'])} {int(d['weight'])} {int(d['port'])} "
                f"{_strip_dot(str(d['target']))}."
            )
        except KeyError as exc:
            raise ValueError(f"missing SRV field {exc}") from exc
    if rtype == "CAA":
        d = r.get("data") or {}
        if d:
            return f'{int(d.get("flags", 0))} {d.get("tag", "issue")} "{d.get("value", "")}"'
        return content
    raise ValueError(f"unsupported type {rtype}")


# --- Cloudflare API client ---------------------------------------------------------


class CloudflareClient:
    def __init__(
        self, token: str, *, base_url: str = API_BASE, http: httpx.AsyncClient | None = None
    ) -> None:
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._http = http

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = self._base + path
        try:
            if self._http is not None:
                resp = await self._http.request(method, url, headers=self._headers, **kwargs)
            else:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.request(method, url, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise CloudflareError(f"Cloudflare API unreachable: {exc.__class__.__name__}") from exc
        try:
            payload = resp.json()
        except ValueError as exc:
            raise CloudflareError(
                f"Cloudflare returned non-JSON (HTTP {resp.status_code})"
            ) from exc
        if not payload.get("success", False):
            messages = (
                "; ".join(str(e.get("message", "")) for e in payload.get("errors", []))
                or f"HTTP {resp.status_code}"
            )
            raise CloudflareError(f"Cloudflare API error: {messages[:300]}")
        return payload

    async def verify_token(self) -> dict[str, Any]:
        """Validate the token itself (GET /user/tokens/verify); raises if invalid."""
        payload = await self._request("GET", "/user/tokens/verify")
        return payload.get("result") or {}

    async def find_zone(self, name: str) -> dict[str, Any] | None:
        payload = await self._request("GET", "/zones", params={"name": name})
        result = payload.get("result") or []
        return result[0] if result else None

    async def list_zones(self) -> list[dict[str, Any]]:
        zones: list[dict[str, Any]] = []
        page = 1
        while page <= 10:  # 10 * 50 zones is plenty for a hosting panel
            payload = await self._request("GET", "/zones", params={"page": page, "per_page": 50})
            zones.extend(payload.get("result") or [])
            info = payload.get("result_info") or {}
            if page >= int(info.get("total_pages") or 1):
                break
            page += 1
        return zones

    async def list_records(self, zone_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        page = 1
        while page <= 10:  # 10 * 100 records is plenty for a hosting zone
            payload = await self._request(
                "GET",
                f"/zones/{zone_id}/dns_records",
                params={"page": page, "per_page": 100},
            )
            records.extend(payload.get("result") or [])
            info = payload.get("result_info") or {}
            if page >= int(info.get("total_pages") or 1):
                break
            page += 1
        return records

    async def create_record(self, zone_id: str, payload: dict[str, Any]) -> None:
        await self._request("POST", f"/zones/{zone_id}/dns_records", json=payload)

    async def update_record(self, zone_id: str, record_id: str, payload: dict[str, Any]) -> None:
        await self._request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=payload)

    async def delete_record(self, zone_id: str, record_id: str) -> None:
        await self._request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")


# --- push algorithm ----------------------------------------------------------------


async def push_zone(
    client: CloudflareClient, zone_name: str, rrsets: list[dict[str, Any]]
) -> PushResult:
    """Push every mappable record of a zone to Cloudflare (create/update/skip)."""
    bare = _strip_dot(zone_name)
    zone = await client.find_zone(bare)
    if zone is None:
        raise ZoneNotInCloudflareError(
            f"Zone {bare} is not in your Cloudflare account — add the domain in the "
            "Cloudflare dashboard first, then push again"
        )
    zone_id = str(zone["id"])
    desired, map_errors = desired_records(bare, rrsets)
    existing = await client.list_records(zone_id)
    result = PushResult(errors=map_errors)

    for d in desired:
        matches = [e for e in existing if e.get("type") == d["type"] and e.get("name") == d["name"]]
        if any(_same(d, e) for e in matches):
            result.skipped += 1
            continue
        single_valued = (
            sum(1 for x in desired if x["type"] == d["type"] and x["name"] == d["name"]) == 1
        )
        try:
            if len(matches) == 1 and single_valued:
                await client.update_record(zone_id, str(matches[0]["id"]), d)
                result.updated += 1
            else:
                await client.create_record(zone_id, d)
                result.created += 1
        except CloudflareError as exc:
            result.errors.append(f"{d['type']} {d['name']}: {exc}")

    log.info(
        "cloudflare_push",
        zone=bare,
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        errors=len(result.errors),
    )
    return result
