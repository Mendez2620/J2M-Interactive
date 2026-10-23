import secrets
from datetime import datetime, timezone
from app.extensions import db


class Customer(db.Model):
    """Customer Loyalty Account Model."""
    __tablename__ = "customers"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(
        db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=True, index=True)
    phone = db.Column(db.String(30), nullable=True, index=True)
    birthdate = db.Column(db.Date, nullable=True)
    email_verified_at = db.Column(db.DateTime, nullable=True)
    terms_accepted_at = db.Column(db.DateTime, nullable=True)
    terms_version = db.Column(db.String(32), nullable=True)
    privacy_accepted_at = db.Column(db.DateTime, nullable=True)
    privacy_version = db.Column(db.String(32), nullable=True)
    marketing_consent = db.Column(db.Boolean, nullable=False, default=False)
    marketing_consent_at = db.Column(db.DateTime, nullable=True)
    consent_ip_hash = db.Column(db.String(64), nullable=True)
    consent_user_agent_hash = db.Column(db.String(64), nullable=True)
    current_points = db.Column(db.Float, default=0.0, nullable=False)
    current_stamps = db.Column(db.Integer, default=0, nullable=False)
    last_earn_at = db.Column(db.DateTime, nullable=True, index=True)
    qr_code_token = db.Column(
        db.String(64),
        unique=True,
        nullable=False,
        index=True,
        default=lambda: secrets.token_urlsafe(32),
    )
    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    business = db.relationship("Business", back_populates="customers")
    transactions = db.relationship(
        "Transaction", back_populates="customer", cascade="all, delete-orphan", lazy="dynamic"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "business_id": self.business_id,
            "full_name": self.full_name,
            "email": self.email,
            "phone": self.phone,
            "birthdate": self.birthdate.isoformat() if self.birthdate else None,
            "current_points": self.current_points,
            "current_stamps": self.current_stamps,
            "qr_code_token": self.qr_code_token,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<Customer id={self.id} full_name={self.full_name} business_id={self.business_id}>"
