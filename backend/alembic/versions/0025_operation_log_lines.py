"""operation_log_lines

Revision ID: 0025_operation_log_lines
Revises: 6776efaa5d6e
Create Date: 2026-06-14 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "0025_operation_log_lines"
down_revision = "a50f8c72bb62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "operations",
        sa.Column("log_lines", sa.Text(), nullable=True, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("operations", "log_lines")
