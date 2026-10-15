import secrets
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CustomerRecoveryChallenge(db.Model):
    __tablename__ = "customer_recovery_challenges"

    id = db.Column(db.String(36), primary_key=True, default=lambda: secrets.token_hex(18))
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"), nullable=True, index=True)
    email_hash = db.Column(db.String(64), nullable=False, index=True)
    ip_hash = db.Column(db.String(64), nullable=False, index=True)
    code_hash = db.Column(db.String(64), nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    consumed_at = db.Column(db.DateTime, nullable=True)

    business = db.relationship("Business")
    customer = db.relationship("Customer")


class CustomerRecoveryRateLimit(db.Model):
    __tablename__ = "customer_recovery_rate_limits"

    scope = db.Column(db.String(16), primary_key=True)
    business_scope = db.Column(db.Integer, primary_key=True)
    key_hash = db.Column(db.String(64), primary_key=True)
    slot = db.Column(db.Integer, primary_key=True)
    reserved_until = db.Column(db.DateTime, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class CustomerRecoveryAuthorization(db.Model):
    __tablename__ = "customer_recovery_authorizations"

    id = db.Column(db.String(36), primary_key=True, default=lambda: secrets.token_hex(18))
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    revoked_at = db.Column(db.DateTime, nullable=True)

    business = db.relationship("Business")
    customer = db.relationship("Customer")
