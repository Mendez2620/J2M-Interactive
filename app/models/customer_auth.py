import secrets
from datetime import datetime, timezone

from sqlalchemy import event

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class CustomerLinkingEnrollment(db.Model):
    __tablename__ = "customer_linking_enrollments"

    id = db.Column(db.String(64), primary_key=True, default=lambda: secrets.token_urlsafe(32))
    business_id = db.Column(db.Integer, nullable=False, index=True)
    customer_id = db.Column(db.Integer, nullable=False, index=True)
    authorized_by_staff_id = db.Column(db.Integer, nullable=False)
    secret_hash = db.Column(db.String(64), nullable=False, unique=True)
    browser_binding_hash = db.Column(db.String(64), nullable=True, unique=True)
    pairing_code_hash = db.Column(db.String(64), nullable=True)
    webauthn_challenge = db.Column(db.LargeBinary, nullable=True)
    status = db.Column(db.String(30), nullable=False, default="pending", index=True)
    recovery_mode = db.Column(db.Boolean, nullable=False, default=False)
    human_check_note = db.Column(db.String(500), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    claimed_at = db.Column(db.DateTime, nullable=True)
    approved_at = db.Column(db.DateTime, nullable=True)
    consumed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        db.CheckConstraint(
            "status IN ('pending','claimed','approved','registration_pending','verifying','completed','cancelled','expired')",
            name="ck_customer_linking_enrollment_status",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            ondelete="RESTRICT", name="fk_customer_linking_customer_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "authorized_by_staff_id"], ["staff.business_id", "staff.id"],
            ondelete="RESTRICT", name="fk_customer_linking_staff_tenant",
        ),
        db.UniqueConstraint("business_id", "id", name="uq_customer_linking_business_id_id"),
    )


class CustomerCredential(db.Model):
    __tablename__ = "customer_credentials"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    customer_id = db.Column(db.Integer, nullable=False, index=True)
    credential_id = db.Column(db.LargeBinary, nullable=False, unique=True)
    public_key = db.Column(db.LargeBinary, nullable=False)
    sign_count = db.Column(db.BigInteger, nullable=False, default=0)
    device_type = db.Column(db.String(30), nullable=True)
    backed_up = db.Column(db.Boolean, nullable=False, default=False)
    transports_json = db.Column(db.Text, nullable=False, default="[]")
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)
    revoked_at = db.Column(db.DateTime, nullable=True)
    revoked_reason = db.Column(db.String(500), nullable=True)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            ondelete="RESTRICT", name="fk_customer_credential_customer_tenant",
        ),
        db.UniqueConstraint("business_id", "id", name="uq_customer_credential_business_id_id"),
    )


class CustomerSession(db.Model):
    __tablename__ = "customer_sessions"

    id = db.Column(db.String(64), primary_key=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    customer_id = db.Column(db.Integer, nullable=False, index=True)
    credential_id = db.Column(db.Integer, nullable=False)
    verifier_hash = db.Column(db.String(64), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    last_active_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            ondelete="RESTRICT", name="fk_customer_session_customer_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "credential_id"], ["customer_credentials.business_id", "customer_credentials.id"],
            ondelete="RESTRICT", name="fk_customer_session_credential_tenant",
        ),
    )


class CustomerAuthChallenge(db.Model):
    __tablename__ = "customer_auth_challenges"

    id = db.Column(db.String(64), primary_key=True, default=lambda: secrets.token_urlsafe(32))
    business_id = db.Column(db.Integer, nullable=False, index=True)
    browser_binding_hash = db.Column(db.String(64), nullable=False, unique=True)
    challenge = db.Column(db.LargeBinary, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    consumed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        db.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="RESTRICT"),
    )


class CustomerAuthAudit(db.Model):
    __tablename__ = "customer_auth_audits"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    customer_id = db.Column(db.Integer, nullable=False, index=True)
    action = db.Column(db.String(50), nullable=False)
    actor_type = db.Column(db.String(30), nullable=False)
    actor_id = db.Column(db.Integer, nullable=True)
    enrollment_id = db.Column(db.String(64), nullable=True)
    credential_id = db.Column(db.Integer, nullable=True)
    reason = db.Column(db.String(500), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)

    __table_args__ = (
        db.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            ondelete="RESTRICT", name="fk_customer_auth_audit_customer_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "enrollment_id"],
            ["customer_linking_enrollments.business_id", "customer_linking_enrollments.id"],
            ondelete="RESTRICT", name="fk_customer_auth_audit_enrollment_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "credential_id"],
            ["customer_credentials.business_id", "customer_credentials.id"],
            ondelete="RESTRICT", name="fk_customer_auth_audit_credential_tenant",
        ),
    )


@event.listens_for(CustomerAuthAudit, "before_update")
@event.listens_for(CustomerAuthAudit, "before_delete")
def _customer_auth_audit_append_only(_mapper, _connection, _target):
    raise ValueError("El historial de autorización de clientes es append-only.")
