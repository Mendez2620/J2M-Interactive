from app.extensions import db
from app.models.platform_admin import PlatformAdmin
from sqlalchemy import event, inspect, update, func


class PlatformAdminSession(db.Model):
    __tablename__ = "platform_admin_sessions"

    token_digest = db.Column(db.String(64), primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("platform_admins.id", ondelete="CASCADE"),
                         nullable=False, index=True)
    password_digest = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime)


class AdminWalletReview(db.Model):
    __tablename__ = "admin_wallet_reviews"
    __table_args__ = (
        db.CheckConstraint("state IN ('prepared','queued','running','finished','expired','rejected','inconclusive')",
                           name="ck_admin_wallet_review_state"),
        db.CheckConstraint(
            "(state NOT IN ('queued', 'running', 'finished', 'inconclusive') OR confirmed_at IS NOT NULL) AND (state != 'queued' OR (owner IS NULL AND claimed_at IS NULL AND finished_at IS NULL)) AND (state NOT IN ('running', 'finished', 'inconclusive') OR (owner IS NOT NULL AND claimed_at IS NOT NULL)) AND (state NOT IN ('finished', 'inconclusive') OR finished_at IS NOT NULL)",
            name="ck_admin_wallet_review_claim",
        ),
        db.Index("ix_admin_wallet_review_queue", "state", "created_at"),
    )

    id = db.Column(db.String(32), primary_key=True)
    session_digest = db.Column(db.String(64), db.ForeignKey("platform_admin_sessions.token_digest"),
                               nullable=False)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id"), nullable=False, index=True)
    candidate_ids = db.Column(db.JSON, nullable=False)
    fingerprint = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    state = db.Column(db.String(16), nullable=False, server_default="prepared")
    confirmed_at = db.Column(db.DateTime)
    claimed_at = db.Column(db.DateTime)
    owner = db.Column(db.String(32))
    finished_at = db.Column(db.DateTime)
    result = db.Column(db.JSON)
    snapshot = db.Column(db.JSON, nullable=False)


@event.listens_for(PlatformAdmin, "after_update")
def revoke_sessions_on_admin_security_change(mapper, connection, admin):
    state = inspect(admin)
    if state.attrs.is_active.history.has_changes() or state.attrs.password_hash.history.has_changes():
        connection.execute(update(PlatformAdminSession.__table__).where(
            PlatformAdminSession.admin_id == admin.id,
            PlatformAdminSession.revoked_at.is_(None),
        ).values(revoked_at=func.now()))
