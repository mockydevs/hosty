"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

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
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
