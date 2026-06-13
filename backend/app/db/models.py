"""ORM models. Every schema change always comes with an Alembic migration."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import utcnow
from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), unique=True, nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="admin")  # admin | client
    # Phase 11a: client accounts get a temp password (forced change), can be
    # suspended, and may carry per-resource quotas (None = unlimited).
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_sites: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_databases: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase 12a: containerized apps quota.
    max_apps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # v2 (ADR-013): stacks quota — replaces max_sites/max_apps at M6.
    max_stacks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase 11d: a plan supplies default quotas; explicit per-user values above
    # always win (see services/quotas.py).
    plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("plans.id", ondelete="SET NULL"), nullable=True
    )
    # Phase 11c: per-client resource limits (None = unlimited).
    max_disk_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_quota_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_max_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase 11d: TOTP 2FA. The secret is Fernet-encrypted (app.core.secrets);
    # it is stored at setup time but 2FA only takes effect once verified
    # (totp_enabled flips to True after the first valid code).
    totp_secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    # Carried by access and proxied-app tokens. Incrementing this value
    # invalidates credentials immediately without timestamp races.
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class SetupState(Base):
    """Singleton row claimed atomically by the first successful setup request."""

    __tablename__ = "setup_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Plan(Base):
    """Phase 11d: a named quota bundle (e.g. Starter 1 site / 1 DB) assignable
    to client accounts instead of raw numbers. None = unlimited."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    max_sites: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_databases: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_apps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_stacks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_disk_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_quota_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_max_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    family_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Site(Base):
    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Phase 11a: every site belongs to a user; clients only ever see their own.
    # Nullable for migration friendliness (legacy rows are backfilled to admin).
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    domain: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    site_user: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    doc_root: Mapped[str] = mapped_column(String(255), nullable=False)
    php_version: Mapped[str] = mapped_column(String(8), nullable=False)
    # provisioning | active | error | deleting
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="provisioning")
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    php_memory_limit: Mapped[str] = mapped_column(String(8), nullable=False, default="256M")
    php_upload_max_filesize: Mapped[str] = mapped_column(String(8), nullable=False, default="64M")
    wordpress: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Phase 11d: set when this site is a staging clone of another site.
    staging_of: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Proxied through Cloudflare (orange cloud): serve an internal origin cert
    # instead of attempting ACME HTTP-01, which the proxy would break.
    behind_cloudflare: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    wp_db_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wp_db_user: Mapped[str | None] = mapped_column(String(32), nullable=True)
    backup_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    backup_frequency: Mapped[str] = mapped_column(String(8), nullable=False, default="daily")
    backup_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    backup_retention: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    backup_last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    backup_include_files: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    backup_include_databases: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    backup_s3_mirror: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class Tenant(Base):
    """v2/M0 (ADR-013): one client account = one Linux user with a fixed,
    panel-allocated subuid/subgid range. This table is the LEDGER — host
    state (useradd, usermod --add-subuids) is derived from it, never the
    other way round. Rows are kept on user deletion until the host user is
    confirmed gone, so a uid range is never silently reissued."""

    __tablename__ = "tenants"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    linux_user: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    uid: Mapped[int | None] = mapped_column(Integer, nullable=True)  # set once provisioned
    subuid_start: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    subuid_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class App(Base):
    """Phase 12a: a containerized app — ONE container, routed through Caddy.

    The container itself is reconstructable from this row (image digest, env,
    ports, volumes); the panel owns the runtime objects via `hosty.*` labels.
    `env_encrypted` is a Fernet-encrypted JSON object (app.core.secrets).
    """

    __tablename__ = "apps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    domain: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    image: Mapped[str] = mapped_column(String(512), nullable=False)  # ref as given
    image_digest: Mapped[str | None] = mapped_column(String(512), nullable=True)  # resolved
    internal_port: Mapped[int] = mapped_column(Integer, nullable=False)
    # Panel-allocated; the container publishes 127.0.0.1:host_port only.
    host_port: Mapped[int] = mapped_column(Integer, unique=True, nullable=False)
    env_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    volumes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # provisioning | running | stopped | error | deleting
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="provisioning")
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class Stack(Base):
    """v2 (ADR-013): one deployable unit a client owns — desired state only.
    The reconciler converges the host toward it; `status` is a cached
    projection for the UI, NEVER an input to planning. Generation semantics
    are K8s-style: API writes bump `generation`; the reconciler sets
    `observed_generation = generation` after convergence."""

    __tablename__ = "stacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)  # slug
    blueprint_id: Mapped[str] = mapped_column(String(32), nullable=False)
    blueprint_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Fernet-encrypted JSON of the user's blueprint inputs (incl. secrets).
    inputs_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # converging | ready | degraded | suspended | deleting | error
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="converging")
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    observed_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class StackService(Base):
    """One container of a stack (v2). Rendered by the blueprint at create/
    upgrade time; the reconciler reads THESE rows, never the blueprint."""

    __tablename__ = "stack_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stack_id: Mapped[int] = mapped_column(
        ForeignKey("stacks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    image: Mapped[str] = mapped_column(String(512), nullable=False)
    image_digest: Mapped[str | None] = mapped_column(String(512), nullable=True)
    internal_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    host_port: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True)
    env_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    memory_mb: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cpu_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_web: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class StackVolume(Base):
    __tablename__ = "stack_volumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stack_id: Mapped[int] = mapped_column(
        ForeignKey("stacks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    service_name: Mapped[str] = mapped_column(String(32), nullable=False)
    mount_path: Mapped[str] = mapped_column(String(255), nullable=False)


class StackEndpoint(Base):
    __tablename__ = "stack_endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stack_id: Mapped[int] = mapped_column(
        ForeignKey("stacks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    domain: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    service_name: Mapped[str] = mapped_column(String(32), nullable=False)
    behind_cloudflare: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Operation(Base):
    """A tracked long-running task (site provisioning/deletion) the UI can poll."""

    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # create_site | delete_site
    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # v2 (ADR-013): stack-scoped operations (create_stack | delete_stack |
    # converge_stack | action:<blueprint>.<action>).
    stack_id: Mapped[int | None] = mapped_column(
        ForeignKey("stacks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    # pending | running | succeeded | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # JSON list of {"name": str, "label": str, "status": "pending|running|done|failed|rolled_back"}
    steps_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Database(Base):
    """A MariaDB database owned by a site. Credentials are shown once at
    creation/reset; only a SHA-256 hash is stored."""

    __tablename__ = "databases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    db_user: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False, default="custom")
    password_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class PanelSetting(Base):
    """Key/value store for panel-wide configuration set through the UI.

    Secrets inside `value` are encrypted with app.core.secrets before storage.
    """

    __tablename__ = "panel_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)  # JSON
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )


class DnsZoneOwner(Base):
    """Phase 11b: maps a PowerDNS zone (external to the panel DB) to the user
    who owns it. Zones without a row are treated as admin-owned (pre-11b)."""

    __tablename__ = "dns_zone_owners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Canonical zone name (trailing dot), unique — one owner per zone.
    zone: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    owner_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Notification(Base):
    """Phase 11d: admin-facing panel notifications (disk nearly full, managed
    service down, backup failed, repeated cert-issuance failures...).

    `dedupe_key` keeps recurring checks from flooding the list: an unresolved
    notification with the same key swallows re-emissions until it's resolved
    (condition cleared) or dismissed.
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False, default="warning")
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AuditLog(Base):
    """Who did what, when (Week 22). One row per mutating API request.

    Request bodies are deliberately NEVER stored — they can contain passwords.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    username: Mapped[str | None] = mapped_column(String(32), nullable=True)
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, index=True
    )


class SshKey(Base):
    """An SSH private key used by Podman quadlets to clone private Git repos."""

    __tablename__ = "ssh_keys"
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_ssh_keys_owner_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    private_key_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ApiToken(Base):
    """Personal Access Tokens for REST API access."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
