import secrets
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import event

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

    __table_args__ = (
        db.UniqueConstraint("business_id", "username", name="uq_promoter_business_username"),
        db.UniqueConstraint("business_id", "id", name="uq_promoters_business_id_id"),
    )

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
    promoter_id = db.Column(db.Integer, nullable=False, index=True)
    customer_id = db.Column(db.Integer, nullable=False, unique=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc), index=True)

    promoter = db.relationship("Promoter", overlaps="customer")
    customer = db.relationship("Customer", overlaps="promoter")
    __table_args__ = (
        db.CheckConstraint("business_id > 0", name="ck_referral_business_positive"),
        db.UniqueConstraint("business_id", "id", name="uq_referral_attribution_business_id_id"),
        db.UniqueConstraint("business_id", "customer_id", name="uq_referral_business_customer"),
        db.ForeignKeyConstraint(
            ["business_id", "promoter_id"], ["promoters.business_id", "promoters.id"],
            ondelete="RESTRICT", name="fk_referral_promoter_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            ondelete="RESTRICT", name="fk_referral_customer_tenant",
        ),
    )


class ReferralEligibility(db.Model):
    """Current anti-fraud review state for one referral attribution."""

    __tablename__ = "referral_eligibilities"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    attribution_id = db.Column(db.Integer, nullable=False, unique=True)
    promoter_id = db.Column(db.Integer, nullable=False, index=True)
    customer_id = db.Column(db.Integer, nullable=False, unique=True)
    status = db.Column(db.String(20), nullable=False, default="pending", index=True)
    source_transaction_id = db.Column(db.Integer, nullable=True, unique=True)
    evidence_reference_hash = db.Column(db.String(64), nullable=True)
    evidence_type = db.Column(db.String(30), nullable=True)
    evidence_key_id = db.Column(db.String(16), nullable=True)
    reviewed_by_staff_id = db.Column(db.Integer, db.ForeignKey("staff.id", ondelete="SET NULL"), nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc),
                           onupdate=lambda: datetime.now(timezone.utc))
    version = db.Column(db.Integer, nullable=False, default=0)

    attribution = db.relationship("ReferralAttribution")
    decisions = db.relationship("ReferralEligibilityDecision", back_populates="eligibility",
                                order_by="ReferralEligibilityDecision.id", passive_deletes=True)

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('pending','in_review','verified','rejected','revoked')",
            name="ck_referral_eligibility_status",
        ),
        db.UniqueConstraint("business_id", "id", name="uq_referral_eligibility_business_id_id"),
        db.UniqueConstraint("business_id", "evidence_reference_hash", name="uq_referral_evidence_business_hash"),
        db.ForeignKeyConstraint(
            ["business_id", "attribution_id"],
            ["referral_attributions.business_id", "referral_attributions.id"],
            ondelete="RESTRICT", name="fk_referral_eligibility_attribution_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "promoter_id"], ["promoters.business_id", "promoters.id"],
            ondelete="RESTRICT", name="fk_referral_eligibility_promoter_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            ondelete="RESTRICT", name="fk_referral_eligibility_customer_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "source_transaction_id"],
            ["transactions.business_id", "transactions.id"],
            ondelete="RESTRICT", name="fk_referral_eligibility_transaction_tenant",
        ),
    )


class ReferralEligibilityDecision(db.Model):
    """Immutable decision history; contains no promoter/customer personal data."""

    __tablename__ = "referral_eligibility_decisions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    eligibility_id = db.Column(db.Integer, nullable=False, index=True)
    actor_staff_id = db.Column(db.Integer, db.ForeignKey("staff.id", ondelete="SET NULL"), nullable=True)
    from_status = db.Column(db.String(20), nullable=False)
    to_status = db.Column(db.String(20), nullable=False)
    source_transaction_id = db.Column(db.Integer, nullable=True)
    evidence_reference_hash = db.Column(db.String(64), nullable=True)
    evidence_type = db.Column(db.String(30), nullable=True)
    evidence_key_id = db.Column(db.String(16), nullable=True)
    reason = db.Column(db.String(500), nullable=False)
    request_id = db.Column(db.String(64), nullable=False)
    request_fingerprint = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    eligibility = db.relationship("ReferralEligibility", back_populates="decisions")

    __table_args__ = (
        db.UniqueConstraint("business_id", "request_id", name="uq_referral_decision_business_request"),
        db.ForeignKeyConstraint(
            ["business_id", "eligibility_id"],
            ["referral_eligibilities.business_id", "referral_eligibilities.id"],
            ondelete="RESTRICT", name="fk_referral_decision_eligibility_tenant",
        ),
    )


@event.listens_for(ReferralEligibilityDecision, "before_update")
@event.listens_for(ReferralEligibilityDecision, "before_delete")
def _referral_decision_is_append_only(_mapper, _connection, _target):
    raise ValueError("El historial de elegibilidad es append-only.")
