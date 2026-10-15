"""add atomic recovery rate limit slots

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
"""
from alembic import op
import sqlalchemy as sa


revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "customer_recovery_rate_limits" not in existing:
        op.create_table(
            "customer_recovery_rate_limits",
            sa.Column("scope", sa.String(length=16), nullable=False),
            sa.Column("business_scope", sa.Integer(), nullable=False),
            sa.Column("key_hash", sa.String(length=64), nullable=False),
            sa.Column("slot", sa.Integer(), nullable=False),
            sa.Column("reserved_until", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("scope", "business_scope", "key_hash", "slot"),
        )
        op.create_index(
            "ix_customer_recovery_rate_limits_reserved_until",
            "customer_recovery_rate_limits", ["reserved_until"],
        )


def downgrade():
    raise RuntimeError("Downgrade bloqueado para preservar los límites de recuperación activos.")
