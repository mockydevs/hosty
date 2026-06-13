"""Add app_slug column to git_sources

Revision ID: 0020_gitsource_app_slug
Revises: e677454aa443
Create Date: 2026-06-14
"""
from alembic import op
import sqlalchemy as sa

revision = '0020_gitsource_app_slug'
down_revision = 'e677454aa443'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('git_sources', sa.Column('app_slug', sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column('git_sources', 'app_slug')
