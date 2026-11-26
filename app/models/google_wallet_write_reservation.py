"""SQLAlchemy metadata for persistent Google Wallet write reservations."""

import sqlalchemy as sa

from app.extensions import db


table = sa.Table(
    "google_wallet_write_reservations",
    db.metadata,
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
    sa.Index("ix_wallet_reservation_business_state", "business_id", "state", "updated_at"),
)
