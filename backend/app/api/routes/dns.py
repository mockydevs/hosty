"""DNS management via PowerDNS (Phase 7): zones/records CRUD + one-click templates.

Phase 11b: zones are tenant-scoped. PowerDNS is external, so ownership lives
in the panel's `dns_zone_owners` table: clients create and manage their own
zones; zones without an ownership row (pre-11b) are admin-owned. Clients get a
404 — never a 403 — for zones they don't own, so existence never leaks.
Cloudflare tokens are per user (services/cloudflare_config.py).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, is_admin
from app.core.config import Settings
from app.core.errors import ConflictError, NotFoundError
from app.db.models import DnsZoneOwner, User
from app.services import cloudflare, cloudflare_config, dns

router = APIRouter(dependencies=[Depends(get_current_user)])

TEMPLATES = ("point-to-server", "external-mail", "google-workspace")


def _settings(request: Request) -> Settings:
    return request.app.state.settings


async def _owned_zones(db: AsyncSession, user: User) -> dict[str, int | None]:
    """canonical zone -> owner_id for every zone this user may see."""
    query = select(DnsZoneOwner)
    if not is_admin(user):
        query = query.where(DnsZoneOwner.owner_id == user.id)
    rows = (await db.execute(query)).scalars().all()
    return {row.zone: row.owner_id for row in rows}


async def _require_zone_access(db: AsyncSession, user: User, zone_id: str) -> str:
    """Validate the user may act on this zone; returns the canonical name.

    Admins may act on every zone (including unowned pre-11b zones); clients
    only on zones they own — anything else is a 404.
    """
    zone = dns.canonical(zone_id)
    if is_admin(user):
        return zone
    row = (
        await db.execute(
            select(DnsZoneOwner).where(DnsZoneOwner.zone == zone, DnsZoneOwner.owner_id == user.id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Zone not found")
    return zone


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
    owner_id: int | None = None
    owner_username: str | None = None


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
    return dns.default_zone_patches(zone, settings.public_ip, settings.dns_default_ttl)


@router.get("/meta", response_model=DnsMetaResponse)
async def dns_meta(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DnsMetaResponse:
    settings = _settings(request)
    return DnsMetaResponse(
        enabled=settings.dns_enabled,
        server_ip=settings.public_ip,
        default_ttl=settings.dns_default_ttl,
        cloudflare_enabled=await cloudflare_config.load_for_user(db, settings, user) is not None,
    )


@router.get("/zones", response_model=list[ZoneSummaryResponse])
async def list_zones(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    zones = await _client(request).list_zones()
    ownership = await _owned_zones(db, user)
    if not is_admin(user):
        return [z for z in zones if dns.canonical(z.name) in ownership]
    usernames = {u.id: u.username for u in (await db.execute(select(User))).scalars().all()}
    out = []
    for z in zones:
        owner_id = ownership.get(dns.canonical(z.name))
        out.append(
            ZoneSummaryResponse(
                id=z.id,
                name=z.name,
                kind=z.kind,
                serial=z.serial,
                owner_id=owner_id,
                owner_username=usernames.get(owner_id) if owner_id is not None else None,
            )
        )
    return out


@router.post("/zones", response_model=ZoneDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_zone(
    request: Request,
    body: CreateZoneRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ZoneDetailResponse:
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
    # Phase 11b: record ownership (admins too — keeps the mapping complete).
    db.add(DnsZoneOwner(zone=dns.canonical(name), owner_id=user.id))
    await db.commit()
    if body.point_to_server:
        await client.patch_rrsets(zone_id, _point_to_server_patches(name, settings))
    return _zone_detail(await client.get_zone(zone_id))


@router.get("/zones/{zone_id}", response_model=ZoneDetailResponse)
async def get_zone(
    request: Request,
    zone_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ZoneDetailResponse:
    await _require_zone_access(db, user, zone_id)
    return _zone_detail(await _client(request).get_zone(zone_id))


@router.delete("/zones/{zone_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_zone(
    request: Request,
    zone_id: str,
    body: DeleteZoneRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    zone = await _require_zone_access(db, user, zone_id)
    if dns.canonical(body.confirm_name) != zone:
        raise ConflictError("Confirmation does not match the zone name")
    await _client(request).delete_zone(zone_id)
    row = (
        await db.execute(select(DnsZoneOwner).where(DnsZoneOwner.zone == zone))
    ).scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()


@router.put("/zones/{zone_id}/records", status_code=status.HTTP_204_NO_CONTENT)
async def upsert_record(
    request: Request,
    zone_id: str,
    body: UpsertRecordRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    await _require_zone_access(db, user, zone_id)
    settings = _settings(request)
    rrset = dns.make_rrset(
        zone_id, body.name, body.type, body.ttl or settings.dns_default_ttl, body.records
    )
    await _client(request).patch_rrsets(zone_id, [dns.replace_patch(rrset)])


@router.delete("/zones/{zone_id}/records", status_code=status.HTTP_204_NO_CONTENT)
async def delete_record(
    request: Request,
    zone_id: str,
    body: DeleteRecordRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    await _require_zone_access(db, user, zone_id)
    patch = dns.delete_patch(zone_id, body.name, body.type)
    if patch["type"] == "NS" and patch["name"] == dns.canonical(zone_id):
        raise ConflictError("Deleting the apex NS record set would break the zone")
    await _client(request).patch_rrsets(zone_id, [patch])


@router.post("/zones/{zone_id}/templates/{template}", status_code=status.HTTP_204_NO_CONTENT)
async def apply_template(
    request: Request,
    zone_id: str,
    template: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    await _require_zone_access(db, user, zone_id)
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


async def _cf_client(request: Request, db: AsyncSession, user: User) -> cloudflare.CloudflareClient:
    """Cloudflare client from the CURRENT USER's stored token (Phase 11b).
    Admins fall back to HOSTY_CLOUDFLARE_API_TOKEN; clients never do."""
    injected = getattr(request.app.state, "cloudflare_client", None)
    if injected is not None:
        return injected
    config = await cloudflare_config.load_for_user(db, _settings(request), user)
    if config is None:
        raise ConflictError("Add a Cloudflare API token in Settings first")
    return cloudflare.CloudflareClient(config.api_token)


@router.post("/zones/{zone_id}/push/cloudflare", response_model=CloudflarePushResponse)
async def push_to_cloudflare(
    request: Request,
    zone_id: str,
    use_own_token: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> CloudflarePushResponse:
    """One click: export every record of this zone to Cloudflare.

    The push uses the ZONE OWNER's token. Admin override (Phase 11b):
    `use_own_token=true` lets the admin push any zone with their own token —
    e.g. onboarding a client whose domain sits in the admin's CF account.
    """
    zone = await _require_zone_access(db, user, zone_id)
    detail = _zone_detail(await _client(request).get_zone(zone_id))

    injected = getattr(request.app.state, "cloudflare_client", None)
    if injected is not None:
        cf = injected
    else:
        settings = _settings(request)
        token_user = user
        if is_admin(user) and not use_own_token:
            # Prefer the zone owner's token when one exists.
            owner_row = (
                await db.execute(select(DnsZoneOwner).where(DnsZoneOwner.zone == zone))
            ).scalar_one_or_none()
            if owner_row is not None and owner_row.owner_id is not None:
                owner = await db.get(User, owner_row.owner_id)
                if owner is not None:
                    token_user = owner
        config = await cloudflare_config.load_for_user(db, settings, token_user)
        if config is None and is_admin(user) and token_user.id != user.id:
            # Owner has no token — the admin's own token is the documented override.
            config = await cloudflare_config.load_for_user(db, settings, user)
        if config is None:
            raise ConflictError(
                "No Cloudflare API token available for this zone's owner — add one in Settings"
            )
        cf = cloudflare.CloudflareClient(config.api_token)

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


# --- Cloudflare account management (token + zones + records) ------------------------


class CloudflareConfigResponse(BaseModel):
    configured: bool
    source: str | None  # db | env


class UpdateCloudflareConfigRequest(BaseModel):
    api_token: str = Field(min_length=10, max_length=256)


class CloudflareZoneResponse(BaseModel):
    id: str
    name: str
    status: str
    paused: bool
    name_servers: list[str]


class CloudflareRecordResponse(BaseModel):
    id: str
    type: str
    name: str
    content: str
    ttl: int
    proxied: bool | None = None
    priority: int | None = None


class UpsertCloudflareRecordRequest(BaseModel):
    type: str = Field(pattern=r"^(A|AAAA|CNAME|TXT|MX|NS)$")
    name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=2048)
    ttl: int = Field(default=1, ge=1, le=86400)  # 1 = Cloudflare "automatic"
    proxied: bool = False
    priority: int | None = Field(default=None, ge=0, le=65535)


def _cf_config_response(config: cloudflare_config.CloudflareConfig | None):
    return CloudflareConfigResponse(
        configured=config is not None, source=config.source if config else None
    )


def _record_response(r: dict) -> CloudflareRecordResponse:
    return CloudflareRecordResponse(
        id=str(r.get("id", "")),
        type=str(r.get("type", "")),
        name=str(r.get("name", "")),
        content=str(r.get("content", "")),
        ttl=int(r.get("ttl") or 1),
        proxied=r.get("proxied"),
        priority=r.get("priority"),
    )


def _record_payload(body: UpsertCloudflareRecordRequest) -> dict:
    payload: dict = {
        "type": body.type,
        "name": body.name.strip().rstrip("."),
        "content": body.content.strip(),
        "ttl": body.ttl,
    }
    if body.type in ("A", "AAAA", "CNAME"):
        payload["proxied"] = body.proxied
    if body.type == "MX":
        payload["priority"] = body.priority if body.priority is not None else 10
    return payload


@router.get("/cloudflare/config", response_model=CloudflareConfigResponse)
async def get_cloudflare_config(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    """Whether the current user has a token and where it came from. Never the token."""
    return _cf_config_response(await cloudflare_config.load_for_user(db, _settings(request), user))


@router.put("/cloudflare/config", response_model=CloudflareConfigResponse)
async def update_cloudflare_config(
    request: Request,
    body: UpdateCloudflareConfigRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    """Verify the token against Cloudflare, then store it for THIS user."""
    token = body.api_token.strip()
    injected = getattr(request.app.state, "cloudflare_client", None)
    cf = injected or cloudflare.CloudflareClient(token)
    await cf.verify_token()  # raises cloudflare_error (502) on a bad token
    saved = await cloudflare_config.save_for_user(db, _settings(request), user, api_token=token)
    return _cf_config_response(saved)


@router.delete("/cloudflare/config", status_code=status.HTTP_204_NO_CONTENT)
async def delete_cloudflare_config(
    db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    if not await cloudflare_config.clear_for_user(db, user):
        raise NotFoundError("No Cloudflare token stored")


@router.get("/cloudflare/zones", response_model=list[CloudflareZoneResponse])
async def list_cloudflare_zones(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    cf = await _cf_client(request, db, user)
    return [
        CloudflareZoneResponse(
            id=str(z.get("id", "")),
            name=str(z.get("name", "")),
            status=str(z.get("status", "")),
            paused=bool(z.get("paused", False)),
            name_servers=[str(ns) for ns in (z.get("name_servers") or [])],
        )
        for z in await cf.list_zones()
    ]


@router.get("/cloudflare/zones/{cf_zone_id}/records", response_model=list[CloudflareRecordResponse])
async def list_cloudflare_records(
    request: Request,
    cf_zone_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Any:
    cf = await _cf_client(request, db, user)
    return [_record_response(r) for r in await cf.list_records(cf_zone_id)]


@router.post(
    "/cloudflare/zones/{cf_zone_id}/records",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def create_cloudflare_record(
    request: Request,
    cf_zone_id: str,
    body: UpsertCloudflareRecordRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    cf = await _cf_client(request, db, user)
    await cf.create_record(cf_zone_id, _record_payload(body))


@router.put(
    "/cloudflare/zones/{cf_zone_id}/records/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def update_cloudflare_record(
    request: Request,
    cf_zone_id: str,
    record_id: str,
    body: UpsertCloudflareRecordRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    cf = await _cf_client(request, db, user)
    await cf.update_record(cf_zone_id, record_id, _record_payload(body))


@router.delete(
    "/cloudflare/zones/{cf_zone_id}/records/{record_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_cloudflare_record(
    request: Request,
    cf_zone_id: str,
    record_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    cf = await _cf_client(request, db, user)
    await cf.delete_record(cf_zone_id, record_id)
