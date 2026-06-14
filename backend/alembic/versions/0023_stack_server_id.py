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
    with op.batch_alter_table("stacks") as batch_op:
        batch_op.add_column(sa.Column("server_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_stacks_server_id"), ["server_id"], unique=False)
        batch_op.create_foreign_key(
            "fk_stacks_server_id",
            "servers",
            ["server_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("stacks") as batch_op:
        batch_op.drop_constraint("fk_stacks_server_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_stacks_server_id"))
        batch_op.drop_column("server_id")
