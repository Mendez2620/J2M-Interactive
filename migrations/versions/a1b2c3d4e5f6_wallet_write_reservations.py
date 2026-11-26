"""Add durable Google Wallet write reservations."""

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d4e5f6"
down_revision = "9d0e1f2a3b4c"
branch_labels = None
depends_on = None
TABLE = "google_wallet_write_reservations"


def _create_table():
    op.create_table(
        TABLE,
        sa.Column("operation_id", sa.String(32), primary_key=True),
        sa.Column("owner_token", sa.String(32), nullable=False),
        sa.Column("issuer", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("business_id", sa.Integer, sa.ForeignKey("businesses.id"), nullable=False),
        sa.Column("customer_id", sa.Integer),
        sa.ForeignKeyConstraint(["business_id", "customer_id"], ["customers.business_id", "customers.id"],
                                name="fk_wallet_reservation_customer_tenant"),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("active_slot", sa.Integer),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("started_at", sa.DateTime),
        sa.Column("remote_resolved_at", sa.DateTime),
        sa.Column("remote_status", sa.Integer),
        sa.CheckConstraint("remote_status BETWEEN 100 AND 599", name="ck_wallet_reservation_status_code"),
        sa.UniqueConstraint("issuer", "kind", "resource_id", "active_slot"),
        sa.CheckConstraint(
            "(kind = 'loyaltyObject' AND customer_id IS NOT NULL) OR "
            "(kind = 'loyaltyClass' AND customer_id IS NULL)",
            name="ck_wallet_reservation_subject",
        ),
        sa.CheckConstraint(
            "(state IN ('reserved','started','uncertain','confirmed') AND "
            "active_slot IS NOT NULL AND active_slot = 1) OR "
            "(state IN ('not_sent','rejected','completed') AND active_slot IS NULL)",
            name="ck_wallet_reservation_active",
        ),
    )
    op.create_index("ix_wallet_reservation_business_state", TABLE, ["business_id", "state", "updated_at"])


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table(TABLE):
        _create_table()
    else:
        # Older bootstrap migrations can start from current db.create_all().
        # Verify its constraints instead of accepting an unrelated partial table.
        columns = {column["name"] for column in inspector.get_columns(TABLE)}
        checks = {check["name"] for check in inspector.get_check_constraints(TABLE)}
        uniques = {tuple(item["column_names"]) for item in inspector.get_unique_constraints(TABLE)}
        foreign_keys = {item["name"] for item in inspector.get_foreign_keys(TABLE)}
        indexes = {item["name"] for item in inspector.get_indexes(TABLE)}
        if (columns != {"operation_id", "owner_token", "issuer", "kind", "resource_id",
                        "business_id", "customer_id", "state", "active_slot", "created_at",
                        "updated_at", "started_at", "remote_resolved_at", "remote_status"}
                or not {"ck_wallet_reservation_subject", "ck_wallet_reservation_active",
                        "ck_wallet_reservation_status_code"} <= checks
                or ("issuer", "kind", "resource_id", "active_slot") not in uniques
                or "fk_wallet_reservation_customer_tenant" not in foreign_keys
                or "ix_wallet_reservation_business_state" not in indexes):
            raise RuntimeError("Existing Wallet reservation schema is incomplete")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE {TABLE} FROM PUBLIC")
        for role in ("anon", "authenticated"):
            op.execute(
                f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
                f"THEN REVOKE ALL ON TABLE {TABLE} FROM {role}; END IF; END $$"
            )


def downgrade():
    if op.get_bind().execute(sa.text(f"SELECT 1 FROM {TABLE} LIMIT 1")).scalar():
        raise RuntimeError("Wallet write history prevents downgrade")
    op.drop_table(TABLE)
