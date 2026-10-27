import secrets
from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CustomerRegistrationChallenge(db.Model):
    __tablename__ = "customer_registration_challenges"

    id = db.Column(db.String(36), primary_key=True, default=lambda: secrets.token_hex(18))
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    promoter_id = db.Column(db.Integer, db.ForeignKey("promoters.id", ondelete="SET NULL"), nullable=True, index=True)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    email_hash = db.Column(db.String(64), nullable=False, index=True)
    phone = db.Column(db.String(30), nullable=False)
    phone_hash = db.Column(db.String(64), nullable=False, index=True)
    birthdate = db.Column(db.Date, nullable=False)
    code_hash = db.Column(db.String(64), nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    terms_version = db.Column(db.String(32), nullable=False)
    terms_accepted_at = db.Column(db.DateTime, nullable=True)
    terms_content_hash = db.Column(db.String(64), nullable=True)
    privacy_version = db.Column(db.String(32), nullable=False)
    privacy_accepted_at = db.Column(db.DateTime, nullable=True)
    privacy_content_hash = db.Column(db.String(64), nullable=True)
    marketing_consent = db.Column(db.Boolean, nullable=False, default=False)
    marketing_consent_at = db.Column(db.DateTime, nullable=True)
    consent_ip_hash = db.Column(db.String(64), nullable=False)
    consent_user_agent_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    consumed_at = db.Column(db.DateTime, nullable=True)


class CustomerRegistrationIdentityClaim(db.Model):
    __tablename__ = "customer_registration_identity_claims"

    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), primary_key=True)
    identity_type = db.Column(db.String(10), primary_key=True)
    identity_hash = db.Column(db.String(64), primary_key=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
