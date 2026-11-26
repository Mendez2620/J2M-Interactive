from datetime import datetime, timezone

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from app.extensions import db
from app.models.business import Business


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class GoogleWalletReferralJob(db.Model):
    __tablename__ = "google_wallet_referral_jobs"
    __table_args__ = (
        db.ForeignKeyConstraint(["business_id", "customer_id"], ["customers.business_id", "customers.id"],
                                ondelete="CASCADE", name="fk_wallet_referral_customer"),
        db.CheckConstraint("status IN ('pending','processing','completed','missing','failed')",
                           name="ck_wallet_referral_status"),
        db.Index("ix_wallet_referral_due", "status", "run_at", "id"),
        db.Index("ix_wallet_referral_business", "business_id", "status"),
    )

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False)
    customer_id = db.Column(db.Integer)
    cursor = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    scheduled_expiry = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    status = db.Column(db.String(16), nullable=False, default="pending", server_default="pending")
    token = db.Column(db.String(32))
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    run_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    locked_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    outcome = db.Column(db.String(24))


@event.listens_for(Session, "before_flush")
def enqueue_business_referral_changes(session, _context, _instances):
    for row in list(session.dirty):
        if isinstance(row, Business):
            state = inspect(row)
            if state.attrs.referrals_enabled.history.has_changes() or state.attrs.is_active.history.has_changes():
                session.add(GoogleWalletReferralJob(business_id=row.id))
