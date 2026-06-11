"""panel settings table + per-site backup scope flags

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-11
"""

import sqlalchemy as sa

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "panel_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.add_column(
        "sites",
        sa.Column("backup_include_files", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.add_column(
        "sites",
        sa.Column("backup_include_databases", sa.Boolean(), nullable=False, server_default="1"),
    )
    op.add_column(
        "sites",
        sa.Column("backup_s3_mirror", sa.Boolean(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("sites", "backup_s3_mirror")
    op.drop_column("sites", "backup_include_databases")
    op.drop_column("sites", "backup_include_files")
    op.drop_table("panel_settings")
