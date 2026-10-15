"""add shared authentication rate limit slots

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
"""
from alembic import op
import sqlalchemy as sa


revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade():
    if "auth_rate_limit_slots" not in set(sa.inspect(op.get_bind()).get_table_names()):
        op.create_table(
            "auth_rate_limit_slots",
            sa.Column("purpose", sa.String(length=24), nullable=False),
            sa.Column("dimension", sa.String(length=16), nullable=False),
            sa.Column("key_hash", sa.String(length=64), nullable=False),
            sa.Column("slot", sa.Integer(), nullable=False),
            sa.Column("reserved_until", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("purpose", "dimension", "key_hash", "slot"),
        )
        op.create_index("ix_auth_rate_limit_slots_reserved_until", "auth_rate_limit_slots", ["reserved_until"])


def downgrade():
    raise RuntimeError("Downgrade bloqueado para preservar el estado de seguridad operativo.")
