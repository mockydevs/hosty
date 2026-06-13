"""Add scopes column to api_tokens

Revision ID: 0021
Revises: 0020_gitsource_app_slug
Create Date: 2026-06-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0021_apitoken_scopes"
down_revision = "0020_gitsource_app_slug"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("api_tokens") as batch_op:
        batch_op.add_column(
            sa.Column(
                "scopes",
                sa.String(128),
                nullable=False,
                server_default="read:write",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("api_tokens") as batch_op:
        batch_op.drop_column("scopes")
