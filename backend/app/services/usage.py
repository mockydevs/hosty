"""Usage metering (Phase 11c + 11d billing groundwork).

Three meters, all computed on demand (no sampling tables):
- disk: `du -sb` per site directory (soft quotas — warnings, never hard stops)
- databases: schema sizes from information_schema (services/mariadb.py)
- bandwidth: summed from Caddy's JSON access log per vhost, filterable by month

The Caddy access log is enabled by `caddy.build_config(access_log_path=…)`;
without it bandwidth simply reports 0 — every function here degrades to zeros
rather than failing, because metering must never break the panel.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import Database, Site, User
from app.services import mariadb
from app.services.sites import site_dir_for
from app.system import fs, runner

log = structlog.get_logger("hosty.usage")


def build_du_argv(site_dir: str, *, root: str) -> list[str]:
    """Pure: `du -sb <dir>` with the path validated inside the sites root."""
    return ["du", "-sb", "--", fs.validate_site_path(site_dir, root=root)]


async def site_disk_bytes(site_dir: str, *, root: str) -> int:
    result = await runner.run(build_du_argv(site_dir, root=root), timeout=120)
    if not result.ok:
        return 0
    head = result.stdout.split("\t", 1)[0].strip()
    return int(head) if head.isdigit() else 0


def _month_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def parse_bandwidth_log(path: str, *, month: str | None = None) -> dict[str, int]:
    """host -> bytes served, from Caddy's JSON access log.

    `month` filters to a YYYY-MM (UTC). Malformed lines are skipped; a missing
    log file yields {} (access logging not enabled / rotated away).
    """
    totals: dict[str, int] = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")  # noqa: SIM115 — closed by `with` below
    except OSError:
        return totals
    with fh:
        for line in fh:
            try:
                entry = json.loads(line)
                host = entry["request"]["host"]
                ts = float(entry["ts"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if month is not None and _month_of(ts) != month:
                continue
            size = entry.get("size", 0)
            if isinstance(size, (int, float)):
                totals[host] = totals.get(host, 0) + int(size)
    return totals


async def _db_sizes_safe() -> dict[str, int]:
    """information_schema sizes, or {} when MariaDB is unreachable — metering
    must never break the panel."""
    try:
        return await mariadb.database_sizes()
    except Exception as exc:
        log.warning("db_size_metering_failed", error=str(exc))
        return {}


@dataclass(frozen=True)
class SiteUsage:
    site_id: int
    domain: str
    disk_bytes: int
    db_bytes: int
    bandwidth_bytes: int


@dataclass(frozen=True)
class ClientUsage:
    user_id: int
    username: str
    site_count: int
    disk_bytes: int
    db_bytes: int
    bandwidth_bytes: int
    max_disk_mb: int | None
    sites: list[SiteUsage] = field(default_factory=list)


async def sites_usage(
    db: AsyncSession,
    settings: Settings,
    sites: list[Site],
    *,
    month: str | None = None,
    db_sizes: dict[str, int] | None = None,
    bandwidth: dict[str, int] | None = None,
) -> list[SiteUsage]:
    """Usage for the given sites. `db_sizes`/`bandwidth` can be precomputed so
    callers iterating many clients pay for them once."""
    if db_sizes is None:
        db_sizes = await _db_sizes_safe()
    if bandwidth is None:
        bandwidth = parse_bandwidth_log(settings.caddy_access_log_path, month=month)
    out: list[SiteUsage] = []
    for site in sites:
        try:
            site_dir = site_dir_for(site.domain, settings)
            disk = await site_disk_bytes(site_dir, root=settings.sites_root)
        except Exception:  # path validation should never fail, but degrade anyway
            disk = 0
        names = (
            (await db.execute(select(Database.name).where(Database.site_id == site.id)))
            .scalars()
            .all()
        )
        out.append(
            SiteUsage(
                site_id=site.id,
                domain=site.domain,
                disk_bytes=disk,
                db_bytes=sum(db_sizes.get(n, 0) for n in names),
                bandwidth_bytes=bandwidth.get(site.domain, 0),
            )
        )
    return out


async def all_clients_usage(
    db: AsyncSession, settings: Settings, *, month: str | None = None
) -> list[ClientUsage]:
    """Per-client usage summary across every user that owns at least one site,
    plus every client account (so empty clients still show up)."""
    from app.services import quotas

    users = (await db.execute(select(User).order_by(User.username))).scalars().all()
    db_sizes = await _db_sizes_safe()
    bandwidth = parse_bandwidth_log(settings.caddy_access_log_path, month=month)
    out: list[ClientUsage] = []
    for user in users:
        sites = (await db.execute(select(Site).where(Site.owner_id == user.id))).scalars().all()
        per_site = await sites_usage(
            db, settings, list(sites), month=month, db_sizes=db_sizes, bandwidth=bandwidth
        )
        limits = await quotas.effective_limits(db, user)
        out.append(
            ClientUsage(
                user_id=user.id,
                username=user.username,
                site_count=len(per_site),
                disk_bytes=sum(s.disk_bytes for s in per_site),
                db_bytes=sum(s.db_bytes for s in per_site),
                bandwidth_bytes=sum(s.bandwidth_bytes for s in per_site),
                max_disk_mb=limits.max_disk_mb,
                sites=per_site,
            )
        )
    return out


def usage_csv(rows: list[ClientUsage], *, month: str | None) -> str:
    """Exportable monthly summary (billing groundwork)."""
    lines = ["username,month,sites,disk_mb,db_mb,bandwidth_mb"]
    for row in rows:
        lines.append(
            f"{row.username},{month or 'all'},{row.site_count},"
            f"{row.disk_bytes // (1024 * 1024)},{row.db_bytes // (1024 * 1024)},"
            f"{row.bandwidth_bytes // (1024 * 1024)}"
        )
    return "\n".join(lines) + "\n"
