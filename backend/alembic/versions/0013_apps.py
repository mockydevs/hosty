"""Containerized apps (Phase 12a): apps table + max_apps quota

Revision ID: 0013
Revises: 0012
Create Date: 2026-06-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("name", sa.String(32), nullable=False, unique=True),
        sa.Column("domain", sa.String(253), nullable=False, unique=True),
        sa.Column("image", sa.String(512), nullable=False),
        sa.Column("image_digest", sa.String(512), nullable=True),
        sa.Column("internal_port", sa.Integer(), nullable=False),
        sa.Column("host_port", sa.Integer(), nullable=False, unique=True),
        sa.Column("env_encrypted", sa.Text(), nullable=True),
        sa.Column("volumes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("memory_mb", sa.Integer(), nullable=True),
        sa.Column("cpu_percent", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="provisioning"),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_apps_owner_id", "apps", ["owner_id"])
    op.add_column("users", sa.Column("max_apps", sa.Integer(), nullable=True))
    op.add_column("plans", sa.Column("max_apps", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("plans", "max_apps")
    op.drop_column("users", "max_apps")
    op.drop_index("ix_apps_owner_id", table_name="apps")
    op.drop_table("apps")
