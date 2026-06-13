"""stack_services.publicly_exposed (external DB connection links)

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-13
"""

import sqlalchemy as sa

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stack_services",
        sa.Column(
            "publicly_exposed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("stack_services", "publicly_exposed")
