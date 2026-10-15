"""allow business-level access audit records

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
"""
from alembic import op
import sqlalchemy as sa


revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"]: column for column in inspector.get_columns("access_audits")}
    if not columns["affected_staff_id"]["nullable"]:
        with op.batch_alter_table("access_audits") as batch:
            batch.alter_column("affected_staff_id", existing_type=sa.Integer(), nullable=True)


def downgrade():
    raise RuntimeError("Downgrade bloqueado para preservar auditorías de acceso a nivel negocio.")
