"""persist stack service Git build source

Revision ID: 0017
Revises: 0016
Create Date: 2026-06-13
"""

import sqlalchemy as sa

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stack_services") as batch:
        batch.add_column(sa.Column("build_repo", sa.String(1024), nullable=True))
        batch.add_column(sa.Column("build_branch", sa.String(255), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("stack_services") as batch:
        batch.drop_column("build_branch")
        batch.drop_column("build_repo")
