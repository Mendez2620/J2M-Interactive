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


@event.listens_for(PlatformAdmin, "after_update")
def revoke_sessions_on_admin_security_change(mapper, connection, admin):
    state = inspect(admin)
    if state.attrs.is_active.history.has_changes() or state.attrs.password_hash.history.has_changes():
        connection.execute(update(PlatformAdminSession.__table__).where(
            PlatformAdminSession.admin_id == admin.id,
            PlatformAdminSession.revoked_at.is_(None),
        ).values(revoked_at=func.now()))
