from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db


class Staff(db.Model):
    """Staff / Cashier / Manager / Admin Model for a Tenant."""
    __tablename__ = "staff"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(
        db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(120), nullable=True)
    username = db.Column(db.String(80), nullable=False)
    email = db.Column(db.String(120), nullable=True)
    phone = db.Column(db.String(30), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="cashier", nullable=False)  # 'admin', 'manager', 'cashier'
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

    def set_password(self, password: str):
        """Hashes and sets the password."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Verifies the password against the stored hash."""
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            "id": self.id,
            "business_id": self.business_id,
            "name": self.name or self.username,
            "username": self.username,
            "email": self.email,
            "phone": self.phone,
            "role": self.role,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<Staff id={self.id} username={self.username} role={self.role} business_id={self.business_id}>"
