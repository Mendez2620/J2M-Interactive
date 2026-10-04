from datetime import datetime, timezone
from app.extensions import db


class AppleDevice(db.Model):
    """Registered Apple Wallet device for APNs push notifications and real-time pass updates."""
    __tablename__ = "apple_devices"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    customer_id = db.Column(
        db.Integer, db.ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True
    )
    device_library_identifier = db.Column(db.String(120), nullable=False, index=True)
    push_token = db.Column(db.String(255), nullable=False)
    pass_type_identifier = db.Column(db.String(120), nullable=False)
    serial_number = db.Column(db.String(120), nullable=False, index=True)
    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    __table_args__ = (
        db.UniqueConstraint(
            "device_library_identifier",
            "pass_type_identifier",
            "serial_number",
            name="uq_device_pass_registration",
        ),
    )

    # Relationships
    customer = db.relationship("Customer", backref=db.backref("apple_devices", cascade="all, delete-orphan", lazy="dynamic"))

    def to_dict(self):
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "device_library_identifier": self.device_library_identifier,
            "push_token": self.push_token,
            "pass_type_identifier": self.pass_type_identifier,
            "serial_number": self.serial_number,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f"<AppleDevice id={self.id} device={self.device_library_identifier} serial={self.serial_number}>"
