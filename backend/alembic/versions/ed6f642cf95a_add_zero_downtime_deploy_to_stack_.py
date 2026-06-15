"""add_zero_downtime_deploy_to_stack_services

Revision ID: ed6f642cf95a
Revises: e6560c0babcc
Create Date: 2026-06-15 04:27:48.986024

MIGRATION SAFETY RULES — read before editing:
  • create_index  → always pass  if_not_exists=True
  • drop_index    → always pass  if_exists=True
  • create_table  → always pass  if_not_exists=True
  • add_column    → wrap in try/except OperationalError for SQLite,
                    or check column existence first for Postgres
  • alter_column  → use batch_alter_table for SQLite compatibility
These guards make migrations idempotent so a partial run or a DB that
already has the object (e.g. from create_tables_on_startup) never
breaks the deploy.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError


revision = 'ed6f642cf95a'
down_revision = 'e6560c0babcc'
branch_labels = None
depends_on = None


def upgrade() -> None:
    try:
        op.add_column('stack_services', sa.Column(
            'zero_downtime_deploy', sa.Boolean(), nullable=False, server_default='0'))
    except OperationalError:
        pass  # already exists (idempotent)


def downgrade() -> None:
    op.drop_column('stack_services', 'zero_downtime_deploy')
