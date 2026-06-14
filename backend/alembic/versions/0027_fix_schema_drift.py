"""fix schema drift after index and log_lines migrations

Revision ID: 0027_fix_schema_drift
Revises: 0026_indexes
Create Date: 2026-06-14 13:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "0027_fix_schema_drift"
down_revision = "0026_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_audit_logs_user_id", table_name="audit_log", if_exists=True)
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"], if_not_exists=True)

    op.execute("UPDATE operations SET log_lines = '' WHERE log_lines IS NULL")
    with op.batch_alter_table("operations") as batch_op:
        batch_op.alter_column(
            "log_lines",
            existing_type=sa.Text(),
            existing_server_default="",
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("operations") as batch_op:
        batch_op.alter_column(
            "log_lines",
            existing_type=sa.Text(),
            existing_server_default="",
            nullable=True,
        )

    op.drop_index("ix_audit_log_user_id", table_name="audit_log", if_exists=True)
    op.create_index("ix_audit_logs_user_id", "audit_log", ["user_id"], if_not_exists=True)
