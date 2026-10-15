from datetime import datetime, timezone

from app.extensions import db


class LoyaltyLedgerEntry(db.Model):
    """Server-generated, append-only record of verified loyalty balance changes."""

    __tablename__ = "loyalty_ledger_entries"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    transaction_id = db.Column(db.Integer, db.ForeignKey("transactions.id", ondelete="RESTRICT"), nullable=True, unique=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id", ondelete="SET NULL"), nullable=True, index=True)
    baseline_customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, unique=True)
    operation_type = db.Column(db.String(20), nullable=False)
    points_delta = db.Column(db.Float, nullable=False, default=0.0)
    stamps_delta = db.Column(db.Integer, nullable=False, default=0)
    points_before = db.Column(db.Float, nullable=False)
    points_after = db.Column(db.Float, nullable=False)
    stamps_before = db.Column(db.Integer, nullable=False)
    stamps_after = db.Column(db.Integer, nullable=False)
    created_at = db.Column(
        db.DateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), index=True,
    )

    __table_args__ = (
        db.CheckConstraint(
            "(operation_type = 'baseline' AND transaction_id IS NULL AND baseline_customer_id IS NOT NULL) OR "
            "(operation_type <> 'baseline' AND transaction_id IS NOT NULL AND baseline_customer_id IS NULL)",
            name="ck_loyalty_ledger_entry_kind",
        ),
    )
