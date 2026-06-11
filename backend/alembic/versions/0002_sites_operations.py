"""sites and operations

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-11
"""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain", sa.String(length=253), nullable=False, unique=True),
        sa.Column("site_user", sa.String(length=32), nullable=False, unique=True),
        sa.Column("doc_root", sa.String(length=255), nullable=False),
        sa.Column("php_version", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("steps_json", sa.Text(), nullable=False),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("operations")
    op.drop_table("sites")
