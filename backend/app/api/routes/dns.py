"""DNS management via PowerDNS (Phase 7): zones/records CRUD + one-click templates."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from app.api.deps import get_current_user
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.services import cloudflare, dns

router = APIRouter(dependencies=[Depends(get_current_user)])

TEMPLATES = ("point-to-server", "external-mail", "google-workspace")


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _client(request: Request) -> dns.PowerDNSClient:
    settings = _settings(request)
    if not settings.dns_enabled:
        raise NotFoundError("DNS management is disabled")
    injected = getattr(request.app.state, "pdns_client", None)
    if injected is not None:
        return injected
    return dns.PowerDNSClient(settings.pdns_api_url, settings.pdns_api_key, settings.pdns_server_id)


class DnsMetaResponse(BaseModel):
    enabled: bool
    server_ip: str
    default_ttl: int
    cloudflare_enabled: bool


class ZoneSummaryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    kind: str
    serial: int


class RRSetResponse(BaseModel):
    name: str
    type: str
    ttl: int
    records: list[str]


class ZoneDetailResponse(BaseModel):
    id: str
    name: str
    serial: int
    rrsets: list[RRSetResponse]


class CreateZoneRequest(BaseModel):
    name: str = Field(min_length=3, max_length=253)
    point_to_server: bool = False


class UpsertRecordRequest(BaseModel):
    name: str = Field(default="@", max_length=254)
    type: str
    ttl: int | None = None
    records: list[str] = Field(min_length=1, max_length=dns.MAX_RECORDS_PER_RRSET)


class DeleteRecordRequest(BaseModel):
    name: str = Field(default="@", max_length=254)
    type: str


class DeleteZoneRequest(BaseModel):
    confirm_name: str


def _zone_detail(raw: dict[str, Any]) -> ZoneDetailResponse:
    rrsets = [
        RRSetResponse(
            name=r["name"],
            type=r["type"],
            ttl=r.get("ttl", 0),
            records=[rec["content"] for rec in r.get("records", [])],
        )
        for r in raw.get("rrsets", [])
        if r.get("type") in (*dns.RECORD_TYPES, "SOA")
    ]
    rrsets.sort(key=lambda r: (r.name, r.type))
    return ZoneDetailResponse(
        id=raw["id"], name=raw["name"], serial=raw.get("serial", 0), rrsets=rrsets
    )


def _point_to_server_patches(zone: str, settings: Settings) -> list[dict[str, Any]]:
    ttl = settings.dns_default_ttl
    return [
        dns.replace_patch(dns.make_rrset(zone, "@", "A", ttl, [settings.public_ip])),
        dns.replace_patch(dns.make_rrset(zone, "www", "CNAME", ttl, [zone])),
    ]


@router.get("/meta", response_model=DnsMetaResponse)
async def dns_meta(request: Request) -> DnsMetaResponse:
    settings = _settings(request)
    return DnsMetaResponse(
        enabled=settings.dns_enabled,
        server_ip=settings.public_ip,
        default_ttl=settings.dns_default_ttl,
        cloudflare_enabled=bool(settings.cloudflare_api_token),
    )


@router.get("/zones", response_model=list[ZoneSummaryResponse])
async def list_zones(request: Request) -> Any:
    return await _client(request).list_zones()


@router.post("/zones", response_model=ZoneDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_zone(request: Request, body: CreateZoneRequest) -> ZoneDetailResponse:
    settings = _settings(request)
    name = dns.validate_zone_name(body.name)
    if body.point_to_server and not settings.public_ip:
        raise ConflictError(
            "Set HOSTY_PUBLIC_IP on the server to use 'point to this server' records"
        )
    client = _client(request)
    created = await client.create_zone(
        name, dns.default_nameservers(name, settings.dns_nameservers)
    )
    zone_id = str(created.get("id", name))
    if body.point_to_server:
        await client.patch_rrsets(zone_id, _point_to_server_patches(name, settings))
    return _zone_detail(await client.get_zone(zone_id))


@router.get("/zones/{zone_id}", response_model=ZoneDetailResponse)
async def get_zone(request: Request, zone_id: str) -> ZoneDetailResponse:
    return _zone_detail(await _client(request).get_zone(zone_id))


@router.delete("/zones/{zone_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_zone(request: Request, zone_id: str, body: DeleteZoneRequest) -> None:
    if dns.canonical(body.confirm_name) != dns.canonical(zone_id):
        raise ConflictError("Confirmation does not match the zone name")
    await _client(request).delete_zone(zone_id)


@router.put("/zones/{zone_id}/records", status_code=status.HTTP_204_NO_CONTENT)
async def upsert_record(request: Request, zone_id: str, body: UpsertRecordRequest) -> None:
    settings = _settings(request)
    rrset = dns.make_rrset(
        zone_id, body.name, body.type, body.ttl or settings.dns_default_ttl, body.records
    )
    await _client(request).patch_rrsets(zone_id, [dns.replace_patch(rrset)])


@router.delete("/zones/{zone_id}/records", status_code=status.HTTP_204_NO_CONTENT)
async def delete_record(request: Request, zone_id: str, body: DeleteRecordRequest) -> None:
    patch = dns.delete_patch(zone_id, body.name, body.type)
    if patch["type"] == "NS" and patch["name"] == dns.canonical(zone_id):
        raise ConflictError("Deleting the apex NS record set would break the zone")
    await _client(request).patch_rrsets(zone_id, [patch])


@router.post("/zones/{zone_id}/templates/{template}", status_code=status.HTTP_204_NO_CONTENT)
async def apply_template(request: Request, zone_id: str, template: str) -> None:
    settings = _settings(request)
    zone = dns.canonical(zone_id)
    ttl = settings.dns_default_ttl
    if template == "point-to-server":
        if not settings.public_ip:
            raise ConflictError("Set HOSTY_PUBLIC_IP on the server to use 'point to this server'")
        patches = _point_to_server_patches(zone, settings)
    elif template == "external-mail":
        patches = [
            dns.replace_patch(dns.make_rrset(zone, "@", "TXT", ttl, ["v=spf1 mx ~all"])),
            dns.replace_patch(dns.make_rrset(zone, "_dmarc", "TXT", ttl, ["v=DMARC1; p=none"])),
        ]
    elif template == "google-workspace":
        patches = [
            dns.replace_patch(dns.make_rrset(zone, "@", "MX", ttl, ["1 smtp.google.com"])),
            dns.replace_patch(
                dns.make_rrset(zone, "@", "TXT", ttl, ["v=spf1 include:_spf.google.com ~all"])
            ),
        ]
    else:
        raise NotFoundError(f"Unknown template: {template} (available: {', '.join(TEMPLATES)})")
    await _client(request).patch_rrsets(zone_id, patches)


class CloudflarePushResponse(BaseModel):
    zone: str
    created: int
    updated: int
    skipped: int
    errors: list[str]


@router.post("/zones/{zone_id}/push/cloudflare", response_model=CloudflarePushResponse)
async def push_to_cloudflare(request: Request, zone_id: str) -> CloudflarePushResponse:
    """One click: export every record of this zone to the Cloudflare account."""
    settings = _settings(request)
    if not settings.cloudflare_api_token:
        raise ConflictError("Set HOSTY_CLOUDFLARE_API_TOKEN to enable Cloudflare push")
    detail = _zone_detail(await _client(request).get_zone(zone_id))
    cf = getattr(request.app.state, "cloudflare_client", None) or cloudflare.CloudflareClient(
        settings.cloudflare_api_token
    )
    result = await cloudflare.push_zone(
        cf, dns.canonical(detail.name), [r.model_dump() for r in detail.rrsets]
    )
    return CloudflarePushResponse(
        zone=detail.name,
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        errors=result.errors,
    )
