"""Add server_id FK to stacks

Revision ID: 0023_stack_server_id
Revises: 0022_servers
Create Date: 2026-06-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_stack_server_id"
down_revision = "0022_servers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stacks", sa.Column("server_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_stacks_server_id"), "stacks", ["server_id"], unique=False)
    op.create_foreign_key(
        "fk_stacks_server_id",
        "stacks",
        "servers",
        ["server_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_stacks_server_id", "stacks", type_="foreignkey")
    op.drop_index(op.f("ix_stacks_server_id"), table_name="stacks")
    op.drop_column("stacks", "server_id")
