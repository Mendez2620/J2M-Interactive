"""scope idempotency keys and persist canonical fingerprints

Revision ID: d5e6f7a8b9c0
Revises: c4f1a2b3d4e5
"""
from alembic import op
import sqlalchemy as sa


revision = "d5e6f7a8b9c0"
down_revision = "c4f1a2b3d4e5"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("transactions")}
    indexes = {index["name"] for index in inspector.get_indexes("transactions")}
    unique_constraints = {item["name"] for item in inspector.get_unique_constraints("transactions")}

    with op.batch_alter_table("transactions") as batch:
        if "request_fingerprint" not in columns:
            batch.add_column(sa.Column("request_fingerprint", sa.String(length=64), nullable=True))
        if "ix_transactions_request_id" in indexes:
            batch.drop_index("ix_transactions_request_id")
        if "uq_transactions_business_request_id" not in unique_constraints:
            batch.create_unique_constraint(
                "uq_transactions_business_request_id", ["business_id", "request_id"]
            )


def downgrade():
    raise RuntimeError(
        "Downgrade bloqueado para no perder fingerprints ni cambiar el alcance de claves idempotentes."
    )
