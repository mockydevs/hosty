"""indexes

Revision ID: 0026_indexes
Revises: 0025_operation_log_lines
Create Date: 2026-06-14 12:01:00.000000
"""
from alembic import op

revision = "0026_indexes"
down_revision = "0025_operation_log_lines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_stacks_status", "stacks", ["status"])
    op.create_index("ix_stacks_owner_id", "stacks", ["owner_id"])
    op.create_index("ix_operations_status", "operations", ["status"])
    op.create_index("ix_operations_stack_id", "operations", ["stack_id"])
    op.create_index("ix_operations_site_id", "operations", ["site_id"])
    op.create_index("ix_audit_logs_user_id", "audit_log", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_stacks_status", "stacks")
    op.drop_index("ix_stacks_owner_id", "stacks")
    op.drop_index("ix_operations_status", "operations")
    op.drop_index("ix_operations_stack_id", "operations")
    op.drop_index("ix_operations_site_id", "operations")
    op.drop_index("ix_audit_logs_user_id", "audit_log")
