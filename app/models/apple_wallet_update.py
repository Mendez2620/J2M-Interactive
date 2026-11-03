from datetime import datetime, timezone

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class ApplePassCredential(db.Model):
    __tablename__ = "apple_pass_credentials"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            ondelete="RESTRICT", name="fk_apple_pass_credential_customer_tenant",
        ),
        db.UniqueConstraint("business_id", "customer_id", name="uq_apple_pass_credential_customer"),
        db.UniqueConstraint("pass_type_identifier", "serial_number", name="uq_apple_pass_identity"),
        db.CheckConstraint("update_version >= 1 AND auth_key_version >= 1", name="ck_apple_pass_versions_positive"),
    )

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    customer_id = db.Column(db.Integer, nullable=False, index=True)
    pass_type_identifier = db.Column(db.String(120), nullable=False)
    serial_number = db.Column(db.String(120), nullable=False, index=True)
    auth_key_version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    auth_token_digest = db.Column(db.String(64), nullable=False)
    update_version = db.Column(db.BigInteger, nullable=False, default=1, server_default="1")
    design_version = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class AppleWalletPublication(db.Model):
    __tablename__ = "apple_wallet_publications"
    __table_args__ = (
        db.UniqueConstraint("business_id", "design_version", "execution_number", name="uq_apple_wallet_publication_execution"),
        db.UniqueConstraint("business_id", "active_key", name="uq_apple_wallet_publication_active"),
        db.UniqueConstraint("business_id", "id", name="uq_apple_wallet_publication_business_id_id"),
        db.CheckConstraint(
            "(status IN ('pending','processing') AND active_key = 'active') OR "
            "(status NOT IN ('pending','processing') AND active_key IS NULL)",
            name="ck_apple_wallet_publication_active_state",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="RESTRICT"), nullable=False, index=True)
    design_version = db.Column(db.Integer, nullable=False)
    execution_number = db.Column(db.Integer, nullable=False, default=1, server_default="1")
    active_key = db.Column(db.String(20), nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending", server_default="pending", index=True)
    created_by_admin_id = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255), nullable=False)
    total_jobs = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class AppleWalletPushJob(db.Model):
    __tablename__ = "apple_wallet_push_jobs"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            ondelete="RESTRICT", name="fk_apple_wallet_push_customer_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "publication_id"], ["apple_wallet_publications.business_id", "apple_wallet_publications.id"],
            ondelete="RESTRICT", name="fk_apple_wallet_push_publication_tenant",
        ),
        db.UniqueConstraint("publication_id", "device_id", name="uq_apple_wallet_publication_device"),
        db.CheckConstraint("attempts >= 0 AND target_update_version >= 1", name="ck_apple_wallet_push_counts"),
    )

    id = db.Column(db.Integer, primary_key=True)
    publication_id = db.Column(db.Integer, nullable=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    customer_id = db.Column(db.Integer, nullable=False, index=True)
    device_id = db.Column(db.Integer, db.ForeignKey("apple_devices.id", ondelete="RESTRICT"), nullable=False, index=True)
    design_version = db.Column(db.Integer, nullable=False)
    target_update_version = db.Column(db.BigInteger, nullable=False)
    reason = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", server_default="pending", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = db.Column(db.DateTime, nullable=True, index=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    lease_token = db.Column(db.String(64), nullable=True)
    last_status_code = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
