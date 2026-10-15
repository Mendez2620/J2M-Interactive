"""add append-only access audit records

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
"""
from alembic import op
import sqlalchemy as sa


revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "access_audits" in inspector.get_table_names():
        return
    op.create_table(
        "access_audits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("platform_admin_id", sa.Integer(), nullable=True),
        sa.Column("actor_staff_id", sa.Integer(), nullable=True),
        sa.Column("affected_staff_id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("previous_state", sa.Text(), nullable=True),
        sa.Column("new_state", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_access_audits_affected_staff_id", "access_audits", ["affected_staff_id"])
    op.create_index("ix_access_audits_business_id", "access_audits", ["business_id"])


def downgrade():
    raise RuntimeError("Downgrade bloqueado para preservar el historial de auditoría de accesos.")
