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
    op.create_index("ix_stacks_status", "stacks", ["status"], if_not_exists=True)
    op.create_index("ix_stacks_owner_id", "stacks", ["owner_id"], if_not_exists=True)
    op.create_index("ix_operations_status", "operations", ["status"], if_not_exists=True)
    op.create_index("ix_operations_stack_id", "operations", ["stack_id"], if_not_exists=True)
    op.create_index("ix_operations_site_id", "operations", ["site_id"], if_not_exists=True)
    op.create_index("ix_audit_logs_user_id", "audit_log", ["user_id"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_stacks_status", "stacks", if_exists=True)
    op.drop_index("ix_stacks_owner_id", "stacks", if_exists=True)
    op.drop_index("ix_operations_status", "operations", if_exists=True)
    op.drop_index("ix_operations_stack_id", "operations", if_exists=True)
    op.drop_index("ix_operations_site_id", "operations", if_exists=True)
    op.drop_index("ix_audit_logs_user_id", "audit_log", if_exists=True)
