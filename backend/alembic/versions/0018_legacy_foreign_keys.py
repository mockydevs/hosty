"""Add foreign keys omitted by legacy SQLite migrations.

Revision ID: 0018
Revises: 0017
Create Date: 2026-06-13
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.create_foreign_key(
            "fk_users_plan_id_plans", "plans", ["plan_id"], ["id"], ondelete="SET NULL"
        )
    with op.batch_alter_table("sites") as batch:
        batch.create_foreign_key(
            "fk_sites_owner_id_users", "users", ["owner_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_foreign_key(
            "fk_sites_staging_of_sites", "sites", ["staging_of"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("sites") as batch:
        batch.drop_constraint("fk_sites_staging_of_sites", type_="foreignkey")
        batch.drop_constraint("fk_sites_owner_id_users", type_="foreignkey")
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_plan_id_plans", type_="foreignkey")
