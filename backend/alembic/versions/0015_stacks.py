"""v2/M4 (ADR-013): stacks + services/volumes/endpoints, operations.stack_id,
max_stacks quota

Revision ID: 0015
Revises: 0014
Create Date: 2026-06-12
"""

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stacks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "owner_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("name", sa.String(32), nullable=False, unique=True),
        sa.Column("blueprint_id", sa.String(32), nullable=False),
        sa.Column("blueprint_version", sa.Integer(), nullable=False),
        sa.Column("inputs_encrypted", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("observed_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "stack_services",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "stack_id",
            sa.Integer(),
            sa.ForeignKey("stacks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("image", sa.String(512), nullable=False),
        sa.Column("image_digest", sa.String(512), nullable=True),
        sa.Column("internal_port", sa.Integer(), nullable=True),
        sa.Column("host_port", sa.Integer(), nullable=True, unique=True),
        sa.Column("env_encrypted", sa.Text(), nullable=True),
        sa.Column("memory_mb", sa.Integer(), nullable=True),
        sa.Column("cpu_percent", sa.Integer(), nullable=True),
        sa.Column("is_web", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "stack_volumes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "stack_id",
            sa.Integer(),
            sa.ForeignKey("stacks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("service_name", sa.String(32), nullable=False),
        sa.Column("mount_path", sa.String(255), nullable=False),
    )
    op.create_table(
        "stack_endpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "stack_id",
            sa.Integer(),
            sa.ForeignKey("stacks.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("domain", sa.String(253), nullable=False, unique=True),
        sa.Column("service_name", sa.String(32), nullable=False),
        sa.Column("behind_cloudflare", sa.Boolean(), nullable=False),
    )
    with op.batch_alter_table("operations") as batch:
        batch.add_column(sa.Column("stack_id", sa.Integer(), nullable=True))
        batch.create_index("ix_operations_stack_id", ["stack_id"])
        batch.create_foreign_key(
            "fk_operations_stack_id", "stacks", ["stack_id"], ["id"], ondelete="SET NULL"
        )
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("max_stacks", sa.Integer(), nullable=True))
    with op.batch_alter_table("plans") as batch:
        batch.add_column(sa.Column("max_stacks", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("plans") as batch:
        batch.drop_column("max_stacks")
    with op.batch_alter_table("users") as batch:
        batch.drop_column("max_stacks")
    with op.batch_alter_table("operations") as batch:
        batch.drop_constraint("fk_operations_stack_id", type_="foreignkey")
        batch.drop_index("ix_operations_stack_id")
        batch.drop_column("stack_id")
    op.drop_table("stack_endpoints")
    op.drop_table("stack_volumes")
    op.drop_table("stack_services")
    op.drop_table("stacks")
