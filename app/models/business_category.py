from datetime import datetime, timezone
from app.extensions import db


class BusinessCategory(db.Model):
    """Category classification for Businesses (e.g. Restaurante, Bar, Cafetería, etc.)."""
    __tablename__ = "business_categories"

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    created_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    businesses = db.relationship("Business", back_populates="category", lazy="dynamic")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<BusinessCategory id={self.id} slug={self.slug}>"
