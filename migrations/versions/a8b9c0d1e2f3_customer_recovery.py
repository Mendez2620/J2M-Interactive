"""add customer recovery challenges and authorizations

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
"""
from alembic import op
import sqlalchemy as sa


revision = "a8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade():
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "customer_recovery_challenges" not in existing:
        op.create_table(
            "customer_recovery_challenges",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("business_id", sa.Integer(), nullable=False),
            sa.Column("customer_id", sa.Integer(), nullable=True),
            sa.Column("email_hash", sa.String(length=64), nullable=False),
            sa.Column("ip_hash", sa.String(length=64), nullable=False),
            sa.Column("code_hash", sa.String(length=64), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        for column in ("business_id", "customer_id", "email_hash", "ip_hash", "created_at", "expires_at"):
            op.create_index(f"ix_customer_recovery_challenges_{column}", "customer_recovery_challenges", [column])
    if "customer_recovery_authorizations" not in existing:
        op.create_table(
            "customer_recovery_authorizations",
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("business_id", sa.Integer(), nullable=False),
            sa.Column("customer_id", sa.Integer(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_customer_recovery_authorizations_business_id", "customer_recovery_authorizations", ["business_id"])
        op.create_index("ix_customer_recovery_authorizations_customer_id", "customer_recovery_authorizations", ["customer_id"])
        op.create_index("ix_customer_recovery_authorizations_expires_at", "customer_recovery_authorizations", ["expires_at"])
        op.create_index("ix_customer_recovery_authorizations_token_hash", "customer_recovery_authorizations", ["token_hash"], unique=True)


def downgrade():
    raise RuntimeError("Downgrade bloqueado para preservar el historial de recuperación de clientes.")
