from datetime import datetime, timezone

from app.extensions import db


class AuthRateLimitSlot(db.Model):
    __tablename__ = "auth_rate_limit_slots"

    purpose = db.Column(db.String(24), primary_key=True)
    dimension = db.Column(db.String(16), primary_key=True)
    key_hash = db.Column(db.String(64), primary_key=True)
    slot = db.Column(db.Integer, primary_key=True)
    reserved_until = db.Column(db.DateTime, nullable=False, index=True)
    updated_at = db.Column(
        db.DateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
