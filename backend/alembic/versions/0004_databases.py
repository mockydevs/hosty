"""databases owned by sites

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-11
"""

import sqlalchemy as sa

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "databases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "site_id",
            sa.Integer(),
            sa.ForeignKey("sites.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("db_user", sa.String(length=64), nullable=False, unique=True),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("password_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("databases")
