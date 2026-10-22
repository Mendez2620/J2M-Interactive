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
    logo_path = db.Column(db.String(255), nullable=True)
    cover_path = db.Column(db.String(255), nullable=True)
    primary_color = db.Column(db.String(7), default="#000000", nullable=False)
    secondary_color = db.Column(db.String(7), default="#FFFFFF", nullable=False)
    visual_theme = db.Column(db.String(20), default="modern", server_default="modern", nullable=False)
    font_style = db.Column(db.String(20), default="modern", server_default="modern", nullable=False)
    text_size = db.Column(db.String(20), default="standard", server_default="standard", nullable=False)
    loyalty_type = db.Column(db.String(20), default="stamps", nullable=False)  # 'stamps' or 'points'
    points_per_currency = db.Column(db.Float, default=1.0, nullable=False)
    stamps_reward_limit = db.Column(db.Integer, default=10, nullable=False)
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)
    cooldown_minutes = db.Column(db.Integer, default=60, nullable=False)  # anti-fraude cooldown
    reward_description = db.Column(db.String(255), default="Recompensa de lealtad", nullable=True)
    wallet_terms = db.Column(db.Text, nullable=True)
    wallet_program_name = db.Column(db.String(60), nullable=True)
    wallet_card_text = db.Column(db.String(120), nullable=True)
    wallet_text_color = db.Column(db.String(7), nullable=True)
    wallet_balance_label = db.Column(db.String(30), nullable=True)
    wallet_instructions = db.Column(db.String(500), nullable=True)
    wallet_contact = db.Column(db.String(160), nullable=True)
    wallet_preset = db.Column(db.String(20), default="classic", server_default="classic", nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    web_access_enabled = db.Column(db.Boolean, default=False, nullable=False)
    mobile_access_enabled = db.Column(db.Boolean, default=False, nullable=False)
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
            "logo_path": self.logo_path,
            "cover_path": self.cover_path,
            "primary_color": self.primary_color,
            "secondary_color": self.secondary_color,
            "visual_theme": self.visual_theme,
            "font_style": self.font_style,
            "text_size": self.text_size,
            "loyalty_type": self.loyalty_type,
            "points_per_currency": self.points_per_currency,
            "stamps_reward_limit": self.stamps_reward_limit,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "cooldown_minutes": self.cooldown_minutes,
            "reward_description": self.reward_description or "Recompensa de lealtad",
            "wallet_terms": self.wallet_terms or f"Presenta esta tarjeta digital en cada visita a {self.name} para acumular beneficios exclusivos.",
            "wallet_program_name": self.wallet_program_name or f"Club {self.name}",
            "wallet_card_text": self.wallet_card_text or "Tu lealtad merece recompensas",
            "wallet_text_color": self.wallet_text_color or "#FFFFFF",
            "wallet_balance_label": self.wallet_balance_label or ("Sellos" if self.loyalty_type == "stamps" else "Puntos"),
            "wallet_instructions": self.wallet_instructions or "Presenta tu tarjeta al realizar tu compra.",
            "wallet_contact": self.wallet_contact,
            "wallet_preset": self.wallet_preset or "classic",
            "is_active": self.is_active,
            "web_access_enabled": self.web_access_enabled,
            "mobile_access_enabled": self.mobile_access_enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f"<Business id={self.id} slug={self.slug} is_active={self.is_active}>"
