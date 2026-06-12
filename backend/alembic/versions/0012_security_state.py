"""Atomic setup and revocable authentication state

Revision ID: 0012
Revises: 0011
Create Date: 2026-06-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users", sa.Column("token_version", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column("refresh_tokens", sa.Column("family_id", sa.String(64), nullable=True))
    op.execute("UPDATE refresh_tokens SET family_id = token_hash WHERE family_id IS NULL")
    with op.batch_alter_table("refresh_tokens") as batch:
        batch.alter_column("family_id", existing_type=sa.String(64), nullable=False)
        batch.create_index("ix_refresh_tokens_family_id", ["family_id"])

    op.create_table(
        "setup_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("completed_at", sa.DateTime(), nullable=False),
    )
    op.execute(
        "INSERT INTO setup_state (id, completed_at) "
        "SELECT 1, CURRENT_TIMESTAMP WHERE EXISTS (SELECT 1 FROM users)"
    )


def downgrade() -> None:
    op.drop_table("setup_state")
    with op.batch_alter_table("refresh_tokens") as batch:
        batch.drop_index("ix_refresh_tokens_family_id")
        batch.drop_column("family_id")
    op.drop_column("users", "token_version")
