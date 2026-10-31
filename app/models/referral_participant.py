import secrets
from datetime import datetime, timezone

from sqlalchemy import event

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class ReferralParticipant(db.Model):
    """Tenant-scoped referral identity; it never represents a loyalty balance."""

    __tablename__ = "referral_participants"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    participant_type = db.Column(db.String(30), nullable=False, index=True)
    promoter_id = db.Column(db.Integer, nullable=True)
    staff_id = db.Column(db.Integer, nullable=True)
    customer_id = db.Column(db.Integer, nullable=True)
    invitation_code = db.Column(
        db.String(48), nullable=False, unique=True,
        default=lambda: secrets.token_urlsafe(24),
    )
    is_enabled = db.Column(db.Boolean, nullable=False, default=False, index=True)
    consent_version = db.Column(db.String(32), nullable=True)
    consented_at = db.Column(db.DateTime, nullable=True)
    consent_evidence_hash = db.Column(db.String(64), nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        db.CheckConstraint(
            "participant_type IN ('external_promoter','staff','customer_ambassador')",
            name="ck_referral_participant_type",
        ),
        db.CheckConstraint(
            "(participant_type = 'external_promoter' AND promoter_id IS NOT NULL AND staff_id IS NULL AND customer_id IS NULL) OR "
            "(participant_type = 'staff' AND promoter_id IS NULL AND staff_id IS NOT NULL AND customer_id IS NULL) OR "
            "(participant_type = 'customer_ambassador' AND promoter_id IS NULL AND staff_id IS NULL AND customer_id IS NOT NULL)",
            name="ck_referral_participant_identity",
        ),
        db.CheckConstraint(
            "participant_type != 'customer_ambassador' OR NOT is_enabled OR "
            "(consent_version IS NOT NULL AND consented_at IS NOT NULL AND consent_evidence_hash IS NOT NULL)",
            name="ck_referral_participant_customer_consent",
        ),
        db.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="RESTRICT",
            name="fk_referral_participant_business",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "promoter_id"], ["promoters.business_id", "promoters.id"],
            ondelete="RESTRICT", name="fk_referral_participant_promoter_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "staff_id"], ["staff.business_id", "staff.id"],
            ondelete="RESTRICT", name="fk_referral_participant_staff_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            ondelete="RESTRICT", name="fk_referral_participant_customer_tenant",
        ),
        db.UniqueConstraint("business_id", "id", name="uq_referral_participant_business_id_id"),
        db.UniqueConstraint("business_id", "promoter_id", name="uq_referral_participant_promoter"),
        db.UniqueConstraint("business_id", "staff_id", name="uq_referral_participant_staff"),
        db.UniqueConstraint("business_id", "customer_id", name="uq_referral_participant_customer"),
    )

    def currently_eligible(self):
        if not self.is_enabled or self.revoked_at is not None:
            return False
        if self.participant_type == "external_promoter":
            return bool(self.promoter and self.promoter.is_active)
        if self.participant_type == "staff":
            return bool(
                self.staff and self.staff.is_active
                and "refer_customers" in (self.staff.permissions or "").split(",")
            )
        return bool(self.customer and self.consented_at and self.consent_evidence_hash)

    promoter = db.relationship("Promoter", overlaps="staff,customer")
    staff = db.relationship("Staff", overlaps="promoter,customer")
    customer = db.relationship("Customer", overlaps="promoter,staff")


class ReferralParticipantAudit(db.Model):
    """Append-only history of referral participation and consent changes."""

    __tablename__ = "referral_participant_audits"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    participant_id = db.Column(db.Integer, nullable=False, index=True)
    action = db.Column(db.String(40), nullable=False)
    actor_type = db.Column(db.String(30), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    previous_state = db.Column(db.Text, nullable=True)
    new_state = db.Column(db.Text, nullable=False)
    request_id = db.Column(db.String(64), nullable=False)
    request_fingerprint = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["business_id", "participant_id"],
            ["referral_participants.business_id", "referral_participants.id"],
            ondelete="RESTRICT", name="fk_referral_participant_audit_tenant",
        ),
        db.UniqueConstraint(
            "business_id", "request_id", name="uq_referral_participant_audit_request"
        ),
    )


@event.listens_for(ReferralParticipantAudit, "before_update")
@event.listens_for(ReferralParticipantAudit, "before_delete")
def _participant_audit_append_only(_mapper, _connection, _target):
    raise ValueError("El historial de participantes de referidos es append-only.")


class CustomerReferralActivation(db.Model):
    """One-use, low-assurance capability delivered inside a Wallet pass."""

    __tablename__ = "customer_referral_activations"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    participant_id = db.Column(db.Integer, nullable=False, index=True)
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    consumed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["business_id", "participant_id"],
            ["referral_participants.business_id", "referral_participants.id"],
            ondelete="RESTRICT", name="fk_customer_referral_activation_participant_tenant",
        ),
        db.UniqueConstraint(
            "business_id", "id", name="uq_customer_referral_activation_business_id_id",
        ),
    )

    participant = db.relationship("ReferralParticipant")
