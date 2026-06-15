"""add_health_check_and_watch_paths

Revision ID: e6560c0babcc
Revises: 6175be6e3b5d
Create Date: 2026-06-15 04:07:07.338741

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


revision = 'e6560c0babcc'
down_revision = '6175be6e3b5d'
branch_labels = None
depends_on = None


def _add_column_safe(table: str, column: sa.Column) -> None:
    """Add a column, silently skipping if it already exists (idempotent)."""
    try:
        op.add_column(table, column)
    except OperationalError:
        pass


def upgrade() -> None:
    # NOT NULL columns must carry server_default so SQLite can back-fill
    # existing rows. The Python-side ORM default is set separately in models.py.
    _add_column_safe('stack_services', sa.Column(
        'health_check_enabled', sa.Boolean(), nullable=False, server_default='0'))
    _add_column_safe('stack_services', sa.Column(
        'health_check_path', sa.String(length=255), nullable=True))
    _add_column_safe('stack_services', sa.Column(
        'health_check_port', sa.Integer(), nullable=True))
    _add_column_safe('stack_services', sa.Column(
        'health_check_interval', sa.Integer(), nullable=False, server_default='10'))
    _add_column_safe('stack_services', sa.Column(
        'health_check_retries', sa.Integer(), nullable=False, server_default='3'))
    _add_column_safe('stack_services', sa.Column(
        'health_check_start_period', sa.Integer(), nullable=False, server_default='30'))
    _add_column_safe('stack_services', sa.Column(
        'health_check_timeout', sa.Integer(), nullable=False, server_default='5'))
    _add_column_safe('stacks', sa.Column('watch_paths_json', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('stacks', 'watch_paths_json')
    op.drop_column('stack_services', 'health_check_timeout')
    op.drop_column('stack_services', 'health_check_start_period')
    op.drop_column('stack_services', 'health_check_retries')
    op.drop_column('stack_services', 'health_check_interval')
    op.drop_column('stack_services', 'health_check_port')
    op.drop_column('stack_services', 'health_check_path')
    op.drop_column('stack_services', 'health_check_enabled')
