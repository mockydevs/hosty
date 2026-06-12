"""ORM models. Schema changes always come with an Alembic migration."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import utcnow
from app.db.base import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="admin")  # admin | client
    # Phase 11a: client accounts get a temp password (forced change), can be
    # suspended, and may carry per-resource quotas (None = unlimited).
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    suspended: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_sites: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_databases: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    password_changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
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


class Operation(Base):
    """A tracked long-running task (site provisioning/deletion) the UI can poll."""

    __tablename__ = "operations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)  # create_site | delete_site
    site_id: Mapped[int | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True, index=True
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
