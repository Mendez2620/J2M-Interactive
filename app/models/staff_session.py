from datetime import datetime, timezone

from app.extensions import db


class StaffSession(db.Model):
    __tablename__ = "staff_sessions"

    id = db.Column(db.String(64), primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("staff.id", ondelete="CASCADE"), nullable=False, index=True)
    channel = db.Column(db.String(10), nullable=False)
    refresh_jti_hash = db.Column(db.String(64), nullable=False, unique=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    revoked_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)

    staff = db.relationship("Staff", back_populates="sessions")

    @property
    def is_revoked(self):
        return self.revoked_at is not None
