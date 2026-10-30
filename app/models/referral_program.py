from datetime import datetime, timezone

from sqlalchemy import event

from app.extensions import db


class ReferralProgramAudit(db.Model):
    """Append-only business referral-program switch history."""

    __tablename__ = "referral_program_audits"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    actor_type = db.Column(db.String(30), nullable=False)
    actor_id = db.Column(db.Integer, nullable=False)
    previous_enabled = db.Column(db.Boolean, nullable=False)
    new_enabled = db.Column(db.Boolean, nullable=False)
    reason = db.Column(db.String(500), nullable=False)
    request_id = db.Column(db.String(64), nullable=False)
    request_fingerprint = db.Column(db.String(64), nullable=False)
    created_at = db.Column(
        db.DateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="RESTRICT",
            name="fk_referral_program_audit_business",
        ),
        db.UniqueConstraint(
            "business_id", "request_id",
            name="uq_referral_program_audit_request",
        ),
    )


@event.listens_for(ReferralProgramAudit, "before_update")
@event.listens_for(ReferralProgramAudit, "before_delete")
def _program_audit_append_only(_mapper, _connection, _target):
    raise ValueError("El historial del programa de referidos es append-only.")
