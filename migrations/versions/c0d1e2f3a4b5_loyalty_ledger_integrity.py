"""add loyalty ledger and atomic cooldown state

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
"""
from alembic import op
import sqlalchemy as sa


revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    customer_columns = {column["name"] for column in inspector.get_columns("customers")}
    if "last_earn_at" not in customer_columns:
        op.add_column("customers", sa.Column("last_earn_at", sa.DateTime(), nullable=True))
        op.create_index("ix_customers_last_earn_at", "customers", ["last_earn_at"])
        op.execute(sa.text(
            "UPDATE customers SET last_earn_at = ("
            "SELECT MAX(transactions.timestamp) FROM transactions "
            "WHERE transactions.customer_id = customers.id "
            "AND transactions.business_id = customers.business_id "
            "AND transactions.type = 'earn')"
        ))

    if "loyalty_ledger_entries" not in tables:
        op.create_table(
            "loyalty_ledger_entries",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("business_id", sa.Integer(), nullable=False),
            sa.Column("customer_id", sa.Integer(), nullable=False),
            sa.Column("transaction_id", sa.Integer(), nullable=True),
            sa.Column("staff_id", sa.Integer(), nullable=True),
            sa.Column("baseline_customer_id", sa.Integer(), nullable=True),
            sa.Column("operation_type", sa.String(length=20), nullable=False),
            sa.Column("points_delta", sa.Float(), nullable=False, server_default="0"),
            sa.Column("stamps_delta", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("points_before", sa.Float(), nullable=False),
            sa.Column("points_after", sa.Float(), nullable=False),
            sa.Column("stamps_before", sa.Integer(), nullable=False),
            sa.Column("stamps_after", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "(operation_type = 'baseline' AND transaction_id IS NULL AND baseline_customer_id IS NOT NULL) OR "
                "(operation_type <> 'baseline' AND transaction_id IS NOT NULL AND baseline_customer_id IS NULL)",
                name="ck_loyalty_ledger_entry_kind",
            ),
            sa.ForeignKeyConstraint(["baseline_customer_id"], ["customers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["staff_id"], ["staff.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"], ondelete="RESTRICT"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("baseline_customer_id", name="uq_ledger_baseline_customer"),
            sa.UniqueConstraint("transaction_id", name="uq_ledger_transaction"),
        )
        for column in ("business_id", "customer_id", "staff_id", "created_at"):
            op.create_index(f"ix_loyalty_ledger_entries_{column}", "loyalty_ledger_entries", [column])


def downgrade():
    raise RuntimeError("Downgrade bloqueado para preservar el ledger de lealtad.")
