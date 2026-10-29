from datetime import datetime, timezone

from sqlalchemy import event, select

from app.extensions import db
from app.models.promoter import ReferralEligibility
from app.models.referral_participant import ReferralParticipant


def _utcnow():
    return datetime.now(timezone.utc)


class ReferralIncentiveRule(db.Model):
    """Tenant-owned, non-executable incentive configuration."""

    __tablename__ = "referral_incentive_rules"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    beneficiary_type = db.Column(db.String(30), nullable=False)
    loyalty_program = db.Column(db.String(20), nullable=False)
    reward_type = db.Column(db.String(30), nullable=False)
    reward_amount = db.Column(db.Numeric(18, 6), nullable=False)
    eligible_condition = db.Column(
        db.String(50), nullable=False, default="first_verified_purchase"
    )
    period_limit = db.Column(db.Integer, nullable=True)
    period_unit = db.Column(db.String(10), nullable=True)
    valid_from = db.Column(db.DateTime, nullable=True)
    valid_until = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="disabled", index=True)
    version = db.Column(db.Integer, nullable=False, default=1)
    created_by_type = db.Column(db.String(30), nullable=False)
    created_by_id = db.Column(db.Integer, nullable=False)
    updated_by_type = db.Column(db.String(30), nullable=False)
    updated_by_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    audits = db.relationship(
        "ReferralIncentiveRuleAudit", back_populates="rule", passive_deletes=True,
        order_by="ReferralIncentiveRuleAudit.id",
    )

    __table_args__ = (
        db.CheckConstraint(
            "beneficiary_type IN ('external_promoter','staff','customer_ambassador')",
            name="ck_referral_incentive_rule_beneficiary",
        ),
        db.CheckConstraint(
            "loyalty_program IN ('points','stamps')",
            name="ck_referral_incentive_rule_program",
        ),
        db.CheckConstraint(
            "reward_type IN ('points','stamps')",
            name="ck_referral_incentive_rule_reward",
        ),
        db.CheckConstraint("reward_amount > 0", name="ck_referral_incentive_rule_amount"),
        db.CheckConstraint(
            "eligible_condition = 'first_verified_purchase'",
            name="ck_referral_incentive_rule_condition",
        ),
        db.CheckConstraint(
            "status IN ('draft','disabled')", name="ck_referral_incentive_rule_status"
        ),
        db.CheckConstraint(
            "(period_limit IS NULL AND period_unit IS NULL) OR "
            "(period_limit > 0 AND period_unit IN ('day','month'))",
            name="ck_referral_incentive_rule_period",
        ),
        db.CheckConstraint(
            "valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from",
            name="ck_referral_incentive_rule_validity",
        ),
        db.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="RESTRICT",
            name="fk_referral_incentive_rule_business",
        ),
        db.UniqueConstraint(
            "business_id", "id", name="uq_referral_incentive_rule_business_id_id"
        ),
    )


class ReferralIncentiveRuleAudit(db.Model):
    """Immutable snapshot of every rule creation or configuration change."""

    __tablename__ = "referral_incentive_rule_audits"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    rule_id = db.Column(db.Integer, nullable=False, index=True)
    rule_version = db.Column(db.Integer, nullable=False)
    action = db.Column(db.String(30), nullable=False)
    actor_type = db.Column(db.String(30), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    previous_snapshot = db.Column(db.Text, nullable=True)
    new_snapshot = db.Column(db.Text, nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    request_id = db.Column(db.String(64), nullable=False)
    request_fingerprint = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    rule = db.relationship("ReferralIncentiveRule", back_populates="audits")

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["business_id", "rule_id"],
            ["referral_incentive_rules.business_id", "referral_incentive_rules.id"],
            ondelete="RESTRICT", name="fk_referral_rule_audit_rule_tenant",
        ),
        db.UniqueConstraint(
            "business_id", "request_id", name="uq_referral_rule_audit_business_request"
        ),
    )


class ReferralIncentiveRecord(db.Model):
    """Immutable dry-run proposal record; never represents a payable grant."""

    __tablename__ = "referral_incentive_records"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    eligibility_id = db.Column(db.Integer, nullable=False, index=True)
    attribution_id = db.Column(db.Integer, nullable=False, index=True)
    source_transaction_id = db.Column(db.Integer, nullable=False, index=True)
    rule_id = db.Column(db.Integer, nullable=False, index=True)
    rule_version = db.Column(db.Integer, nullable=False)
    beneficiary_type = db.Column(db.String(30), nullable=False)
    beneficiary_id = db.Column(db.Integer, nullable=False)
    event_key = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="proposed")
    reverses_record_id = db.Column(db.Integer, nullable=True)
    reason = db.Column(db.String(500), nullable=False)
    audit_reference = db.Column(db.String(120), nullable=False)
    created_by_type = db.Column(db.String(30), nullable=False)
    created_by_id = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        db.CheckConstraint(
            "beneficiary_type IN ('external_promoter','staff','customer_ambassador')",
            name="ck_referral_incentive_record_beneficiary",
        ),
        db.CheckConstraint(
            "status IN ('proposed','voided')",
            name="ck_referral_incentive_record_status",
        ),
        db.CheckConstraint(
            "(status = 'proposed' AND reverses_record_id IS NULL) OR "
            "(status = 'voided' AND reverses_record_id IS NOT NULL)",
            name="ck_referral_incentive_record_reversal",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "eligibility_id"],
            ["referral_eligibilities.business_id", "referral_eligibilities.id"],
            ondelete="RESTRICT", name="fk_referral_incentive_record_eligibility_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "attribution_id"],
            ["referral_attributions.business_id", "referral_attributions.id"],
            ondelete="RESTRICT", name="fk_referral_incentive_record_attribution_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "source_transaction_id"],
            ["transactions.business_id", "transactions.id"],
            ondelete="RESTRICT", name="fk_referral_incentive_record_transaction_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "rule_id"],
            ["referral_incentive_rules.business_id", "referral_incentive_rules.id"],
            ondelete="RESTRICT", name="fk_referral_incentive_record_rule_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "reverses_record_id"],
            ["referral_incentive_records.business_id", "referral_incentive_records.id"],
            ondelete="RESTRICT", name="fk_referral_incentive_record_reversal_tenant",
        ),
        db.UniqueConstraint(
            "business_id", "id", name="uq_referral_incentive_record_business_id_id"
        ),
        db.UniqueConstraint(
            "business_id", "event_key", name="uq_referral_incentive_record_event"
        ),
        db.UniqueConstraint(
            "business_id", "source_transaction_id", "beneficiary_type", "status",
            name="uq_referral_incentive_record_subject",
        ),
    )


@event.listens_for(ReferralIncentiveRuleAudit, "before_update")
@event.listens_for(ReferralIncentiveRuleAudit, "before_delete")
@event.listens_for(ReferralIncentiveRecord, "before_update")
@event.listens_for(ReferralIncentiveRecord, "before_delete")
def _referral_incentive_history_is_append_only(_mapper, _connection, _target):
    raise ValueError("El historial de incentivos es append-only.")


@event.listens_for(ReferralIncentiveRecord, "before_insert")
def _referral_incentive_subject_must_match(_mapper, connection, target):
    eligibility = ReferralEligibility.__table__
    participant = ReferralParticipant.__table__
    if target.beneficiary_type == "external_promoter":
        legacy = select(eligibility.c.id).where(
            eligibility.c.business_id == target.business_id,
            eligibility.c.id == target.eligibility_id,
            eligibility.c.attribution_id == target.attribution_id,
            eligibility.c.source_transaction_id == target.source_transaction_id,
            eligibility.c.promoter_id == target.beneficiary_id,
        )
        if connection.execute(legacy).first() is not None:
            return
    query = select(eligibility.c.id).where(
        eligibility.c.business_id == target.business_id,
        eligibility.c.id == target.eligibility_id,
        eligibility.c.attribution_id == target.attribution_id,
        eligibility.c.source_transaction_id == target.source_transaction_id,
    )
    query = query.join(
        participant,
        (participant.c.business_id == eligibility.c.business_id)
        & (participant.c.id == eligibility.c.participant_id),
    ).where(participant.c.participant_type == target.beneficiary_type)
    identity_column = {
        "external_promoter": participant.c.promoter_id,
        "staff": participant.c.staff_id,
        "customer_ambassador": participant.c.customer_id,
    }.get(target.beneficiary_type)
    if identity_column is None:
        raise ValueError("Tipo de beneficiario inválido.")
    query = query.where(identity_column == target.beneficiary_id)
    if connection.execute(query).first() is None:
        raise ValueError("La identidad económica del incentivo no coincide con la elegibilidad.")
