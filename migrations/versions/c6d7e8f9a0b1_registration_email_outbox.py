"""add durable registration email outbox

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
"""
from alembic import op
import sqlalchemy as sa


revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("registration_email_jobs"):
        return
    op.create_table(
        "registration_email_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("challenge_id", sa.String(36), nullable=False),
        sa.Column("encrypted_code", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_error_type", sa.String(80), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["challenge_id"], ["customer_registration_challenges.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("challenge_id", name="uq_registration_email_challenge"),
    )
    op.create_index("ix_registration_email_business", "registration_email_jobs", ["business_id"])
    op.create_index("ix_registration_email_status_due", "registration_email_jobs", ["status", "next_attempt_at"])


def downgrade():
    raise RuntimeError("Downgrade bloqueado: eliminar el outbox puede perder correos de registro pendientes.")
