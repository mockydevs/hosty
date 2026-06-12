"""Phase 11a multi-tenancy: site ownership + client-account fields/quotas

Revision ID: 0009
Revises: 0008
Create Date: 2026-06-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.add_column("users", sa.Column("suspended", sa.Boolean(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("max_sites", sa.Integer(), nullable=True))
    op.add_column("users", sa.Column("max_databases", sa.Integer(), nullable=True))

    op.add_column("sites", sa.Column("owner_id", sa.Integer(), nullable=True))
    op.create_index("ix_sites_owner_id", "sites", ["owner_id"])
    # Existing sites belong to the (oldest) admin account.
    op.execute(
        "UPDATE sites SET owner_id = ("
        "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1)"
    )


def downgrade() -> None:
    op.drop_index("ix_sites_owner_id", table_name="sites")
    op.drop_column("sites", "owner_id")
    op.drop_column("users", "max_databases")
    op.drop_column("users", "max_sites")
    op.drop_column("users", "suspended")
    op.drop_column("users", "must_change_password")
