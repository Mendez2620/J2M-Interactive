"""add persistent Google Wallet synchronization outbox

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
"""
from alembic import op
import sqlalchemy as sa


revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade():
    if sa.inspect(op.get_bind()).has_table("google_wallet_sync_jobs"):
        return
    op.create_table(
        "google_wallet_sync_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("transaction_id", name="uq_google_wallet_sync_transaction"),
    )
    op.create_index("ix_google_wallet_sync_business", "google_wallet_sync_jobs", ["business_id"])
    op.create_index("ix_google_wallet_sync_customer", "google_wallet_sync_jobs", ["customer_id"])
    op.create_index("ix_google_wallet_sync_status_due", "google_wallet_sync_jobs", ["status", "next_attempt_at"])


def downgrade():
    raise RuntimeError("Downgrade bloqueado: eliminar el outbox puede perder sincronizaciones pendientes.")
