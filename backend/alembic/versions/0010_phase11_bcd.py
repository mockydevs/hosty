"""Phase 11b/c/d: zone ownership, plans, resource limits, 2FA, notifications, staging

Revision ID: 0010
Revises: 0009
Create Date: 2026-06-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Phase 11d: named quota bundles.
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("max_sites", sa.Integer(), nullable=True),
        sa.Column("max_databases", sa.Integer(), nullable=True),
        sa.Column("max_disk_mb", sa.Integer(), nullable=True),
        sa.Column("cpu_quota_percent", sa.Integer(), nullable=True),
        sa.Column("memory_max_mb", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    # Phase 11b: PowerDNS zones are external — map zone name -> owning user.
    op.create_table(
        "dns_zone_owners",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("zone", sa.String(255), nullable=False, unique=True),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_dns_zone_owners_owner_id", "dns_zone_owners", ["owner_id"])

    # Phase 11d: admin notifications.
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False, server_default="warning"),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("dedupe_key", sa.String(128), nullable=True),
        sa.Column("read", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_notifications_dedupe_key", "notifications", ["dedupe_key"])
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])

    # Users: plan, per-client resource limits (11c), TOTP 2FA (11d).
    # Like 0009's sites.owner_id: plain Integer, no inline FK — SQLite cannot
    # ALTER constraints in (the relationship lives in the ORM model).
    op.add_column("users", sa.Column("plan_id", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("max_disk_mb", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("cpu_quota_percent", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("memory_max_mb", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("totp_secret_encrypted", sa.Text(), nullable=True))
    op.add_column(
        "users", sa.Column("totp_enabled", sa.Boolean(), nullable=False, server_default="0")
    )

    # Sites: staging clone link (11d).
    op.add_column("sites", sa.Column("staging_of", sa.Integer(), nullable=True))
    op.create_index("ix_sites_staging_of", "sites", ["staging_of"])


def downgrade() -> None:
    op.drop_index("ix_sites_staging_of", table_name="sites")
    op.drop_column("sites", "staging_of")
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret_encrypted")
    op.drop_column("users", "memory_max_mb")
    op.drop_column("users", "cpu_quota_percent")
    op.drop_column("users", "max_disk_mb")
    op.drop_column("users", "plan_id")
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_dedupe_key", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_dns_zone_owners_owner_id", table_name="dns_zone_owners")
    op.drop_table("dns_zone_owners")
    op.drop_table("plans")
