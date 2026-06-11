"""per-site backup schedule columns

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-11
"""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sites",
        sa.Column("backup_enabled", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.add_column(
        "sites",
        sa.Column("backup_frequency", sa.String(8), nullable=False, server_default="daily"),
    )
    op.add_column(
        "sites", sa.Column("backup_hour", sa.Integer(), nullable=False, server_default="3")
    )
    op.add_column(
        "sites", sa.Column("backup_retention", sa.Integer(), nullable=False, server_default="7")
    )
    op.add_column("sites", sa.Column("backup_last_run_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("sites", "backup_last_run_at")
    op.drop_column("sites", "backup_retention")
    op.drop_column("sites", "backup_hour")
    op.drop_column("sites", "backup_frequency")
    op.drop_column("sites", "backup_enabled")
