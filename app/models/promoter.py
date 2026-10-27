import secrets
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db


class Promoter(db.Model):
    __tablename__ = "promoters"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    username = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    referral_code = db.Column(db.String(32), nullable=False, unique=True, default=lambda: secrets.token_urlsafe(18))
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    auth_version = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    business = db.relationship("Business", back_populates="promoters")
    sessions = db.relationship("PromoterSession", back_populates="promoter", cascade="all, delete-orphan", passive_deletes=True)

    __table_args__ = (db.UniqueConstraint("business_id", "username", name="uq_promoter_business_username"),)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class PromoterSession(db.Model):
    __tablename__ = "promoter_sessions"

    id = db.Column(db.String(64), primary_key=True)
    promoter_id = db.Column(db.Integer, db.ForeignKey("promoters.id", ondelete="CASCADE"), nullable=False, index=True)
    verifier_hash = db.Column(db.String(64), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    promoter = db.relationship("Promoter", back_populates="sessions")


class ReferralAttribution(db.Model):
    __tablename__ = "referral_attributions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    promoter_id = db.Column(db.Integer, db.ForeignKey("promoters.id", ondelete="RESTRICT"), nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    promoter = db.relationship("Promoter")
    customer = db.relationship("Customer")
    __table_args__ = (
        db.CheckConstraint("business_id > 0", name="ck_referral_business_positive"),
        db.UniqueConstraint("business_id", "customer_id", name="uq_referral_business_customer"),
    )
