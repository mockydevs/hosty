"""persist complete stack service build configuration

Revision ID: 0028_stack_build_config
Revises: 0027_fix_schema_drift
Create Date: 2026-06-15
"""

import sqlalchemy as sa

from alembic import op

revision = "0028_stack_build_config"
down_revision = "0027_fix_schema_drift"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stack_services") as batch:
        batch.add_column(
            sa.Column("build_tool", sa.String(16), nullable=False, server_default="dockerfile")
        )
        batch.add_column(
            sa.Column("build_context", sa.String(255), nullable=False, server_default=".")
        )
        batch.add_column(
            sa.Column(
                "dockerfile_path", sa.String(255), nullable=False, server_default="Dockerfile"
            )
        )
        batch.add_column(sa.Column("build_args_encrypted", sa.Text(), nullable=True))
        batch.add_column(sa.Column("depends_on_json", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("stack_services") as batch:
        batch.drop_column("depends_on_json")
        batch.drop_column("build_args_encrypted")
        batch.drop_column("dockerfile_path")
        batch.drop_column("build_context")
        batch.drop_column("build_tool")
