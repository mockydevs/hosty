"""per-site Cloudflare proxy flag (internal origin certificate)

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sites",
        sa.Column("behind_cloudflare", sa.Boolean(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("sites", "behind_cloudflare")
