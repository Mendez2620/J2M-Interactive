from datetime import datetime, timezone

from app.extensions import db


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class RegistrationEmailJob(db.Model):
    __tablename__ = "registration_email_jobs"

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    challenge_id = db.Column(db.String(36), db.ForeignKey("customer_registration_challenges.id", ondelete="CASCADE"), nullable=False, unique=True)
    encrypted_code = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending", server_default="pending", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = db.Column(db.DateTime, nullable=True, index=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    lease_token = db.Column(db.String(64), nullable=True)
    sent_at = db.Column(db.DateTime, nullable=True)
    last_error_type = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    challenge = db.relationship("CustomerRegistrationChallenge")
