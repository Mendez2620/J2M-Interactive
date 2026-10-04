from datetime import datetime, timezone
from app.extensions import db


class Staff(db.Model):
    """Staff / Cashier / Admin Model for a Tenant."""
    __tablename__ = "staff"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(
        db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    username = db.Column(db.String(80), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="cashier", nullable=False)  # 'admin', 'cashier'
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Unique constraint per business (same username can exist across different businesses)
    __table_args__ = (
        db.UniqueConstraint("business_id", "username", name="uq_business_username"),
    )

    # Relationships
    business = db.relationship("Business", back_populates="staff_members")
    transactions = db.relationship(
        "Transaction", back_populates="staff", lazy="dynamic"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "business_id": self.business_id,
            "username": self.username,
            "role": self.role,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<Staff id={self.id} username={self.username} business_id={self.business_id} role={self.role}>"
