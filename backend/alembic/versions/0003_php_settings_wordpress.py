"""per-site PHP settings + WordPress install state

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-11
"""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sites") as batch:
        batch.add_column(
            sa.Column("php_memory_limit", sa.String(8), nullable=False, server_default="256M")
        )
        batch.add_column(
            sa.Column("php_upload_max_filesize", sa.String(8), nullable=False, server_default="64M")
        )
        batch.add_column(
            sa.Column("wordpress", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("wp_db_name", sa.String(64), nullable=True))
        batch.add_column(sa.Column("wp_db_user", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("sites") as batch:
        batch.drop_column("wp_db_user")
        batch.drop_column("wp_db_name")
        batch.drop_column("wordpress")
        batch.drop_column("php_upload_max_filesize")
        batch.drop_column("php_memory_limit")
