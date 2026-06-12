"""v2/M0 (ADR-013): tenant ledger — Linux user + subuid range per client

Revision ID: 0014
Revises: 0013
Create Date: 2026-06-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("linux_user", sa.String(32), nullable=False, unique=True),
        sa.Column("uid", sa.Integer(), nullable=True),
        sa.Column("subuid_start", sa.Integer(), nullable=False, unique=True),
        sa.Column("subuid_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tenants")
