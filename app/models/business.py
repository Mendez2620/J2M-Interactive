from datetime import datetime, timezone
from app.extensions import db


class Business(db.Model):
    """Business (Tenant) Model for SaaS Multi-Tenant Platform."""
    __tablename__ = "businesses"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(120), unique=True, nullable=False, index=True)
    category_id = db.Column(
        db.Integer, db.ForeignKey("business_categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    logo_url = db.Column(db.String(255), nullable=True)
    primary_color = db.Column(db.String(7), default="#000000", nullable=False)
    secondary_color = db.Column(db.String(7), default="#FFFFFF", nullable=False)
    loyalty_type = db.Column(db.String(20), default="stamps", nullable=False)  # 'stamps' or 'points'
    points_per_currency = db.Column(db.Float, default=1.0, nullable=False)
    stamps_reward_limit = db.Column(db.Integer, default=10, nullable=False)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    category = db.relationship("BusinessCategory", back_populates="businesses")
    customers = db.relationship(
        "Customer", back_populates="business", cascade="all, delete-orphan", lazy="dynamic"
    )
    staff_members = db.relationship(
        "Staff", back_populates="business", cascade="all, delete-orphan", lazy="dynamic"
    )
    transactions = db.relationship(
        "Transaction", back_populates="business", cascade="all, delete-orphan", lazy="dynamic"
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "category_id": self.category_id,
            "category_name": self.category.name if self.category else "Sin categoría",
            "logo_url": self.logo_url,
            "primary_color": self.primary_color,
            "secondary_color": self.secondary_color,
            "loyalty_type": self.loyalty_type,
            "points_per_currency": self.points_per_currency,
            "stamps_reward_limit": self.stamps_reward_limit,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<Business id={self.id} slug={self.slug} is_active={self.is_active}>"
