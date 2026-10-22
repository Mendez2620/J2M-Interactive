"""fence registration email worker leases

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
"""
from alembic import op
import sqlalchemy as sa


revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"]: column for column in inspector.get_columns("registration_email_jobs")}
    with op.batch_alter_table("registration_email_jobs") as batch:
        if "lease_token" not in columns:
            batch.add_column(sa.Column("lease_token", sa.String(64), nullable=True))
        if not columns["encrypted_code"]["nullable"]:
            batch.alter_column("encrypted_code", existing_type=sa.Text(), nullable=True)


def downgrade():
    raise RuntimeError("Downgrade bloqueado: retirar el fencing permitiría que workers obsoletos sobrescriban trabajos.")
