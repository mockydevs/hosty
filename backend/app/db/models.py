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
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="admin")
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
    wp_db_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wp_db_user: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
