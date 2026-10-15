from datetime import datetime, timezone

from app.extensions import db


class AccessAudit(db.Model):
    """Append-only record of sensitive access administration."""

    __tablename__ = "access_audits"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    action = db.Column(db.String(50), nullable=False)
    # Identifiers are intentionally not foreign keys: audit history must survive
    # later deletion of an administrator, employee, or tenant record.
    platform_admin_id = db.Column(db.Integer, nullable=True)
    actor_staff_id = db.Column(db.Integer, nullable=True)
    affected_staff_id = db.Column(db.Integer, nullable=True, index=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    previous_state = db.Column(db.Text, nullable=True)
    new_state = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
