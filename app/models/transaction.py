from datetime import datetime, timezone
from app.extensions import db


class Transaction(db.Model):
    """Loyalty Transaction Model (points/stamps accrual or redemption)."""
    __tablename__ = "transactions"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    business_id = db.Column(
        db.Integer, db.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    staff_id = db.Column(
        db.Integer, db.ForeignKey("staff.id", ondelete="SET NULL"), nullable=True, index=True
    )
    type = db.Column(db.String(20), nullable=False)  # 'earn', 'redeem'
    points_amount = db.Column(db.Float, default=0.0, nullable=False)
    stamps_amount = db.Column(db.Integer, default=0, nullable=False)
    purchase_amount = db.Column(db.Float, default=0.0, nullable=False)
    timestamp = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    # Relationships
    business = db.relationship("Business", back_populates="transactions")
    customer = db.relationship("Customer", back_populates="transactions")
    staff = db.relationship("Staff", back_populates="transactions")

    def to_dict(self):
        return {
            "id": self.id,
            "business_id": self.business_id,
            "customer_id": self.customer_id,
            "staff_id": self.staff_id,
            "type": self.type,
            "points_amount": self.points_amount,
            "stamps_amount": self.stamps_amount,
            "purchase_amount": self.purchase_amount,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
        }

    def __repr__(self):
        return (
            f"<Transaction id={self.id} type={self.type} "
            f"business_id={self.business_id} customer_id={self.customer_id}>"
        )
