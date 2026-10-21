"""add verified customer registration and consent evidence

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
"""
from alembic import op
import sqlalchemy as sa

revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    customer_columns = {column["name"] for column in inspector.get_columns("customers")}
    additions = (
        ("email_verified_at", sa.DateTime()), ("terms_accepted_at", sa.DateTime()),
        ("terms_version", sa.String(32)), ("privacy_accepted_at", sa.DateTime()),
        ("privacy_version", sa.String(32)),
        ("marketing_consent_at", sa.DateTime()), ("consent_ip_hash", sa.String(64)),
        ("consent_user_agent_hash", sa.String(64)),
    )
    for name, type_ in additions:
        if name not in customer_columns:
            op.add_column("customers", sa.Column(name, type_, nullable=True))
    if "marketing_consent" not in customer_columns:
        op.add_column("customers", sa.Column("marketing_consent", sa.Boolean(), nullable=False, server_default=sa.false()))

    if not inspector.has_table("customer_registration_challenges"):
        op.create_table(
            "customer_registration_challenges",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("business_id", sa.Integer(), nullable=False),
            sa.Column("full_name", sa.String(120), nullable=False),
            sa.Column("email", sa.String(120), nullable=False),
            sa.Column("email_hash", sa.String(64), nullable=False),
            sa.Column("phone", sa.String(30), nullable=False),
            sa.Column("phone_hash", sa.String(64), nullable=False),
            sa.Column("birthdate", sa.Date(), nullable=False),
            sa.Column("code_hash", sa.String(64), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("terms_version", sa.String(32), nullable=False),
            sa.Column("privacy_version", sa.String(32), nullable=False),
            sa.Column("marketing_consent", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("consent_ip_hash", sa.String(64), nullable=False),
            sa.Column("consent_user_agent_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("consumed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_registration_challenge_business", "customer_registration_challenges", ["business_id"])
        op.create_index("ix_registration_challenge_email", "customer_registration_challenges", ["email_hash"])
        op.create_index("ix_registration_challenge_phone", "customer_registration_challenges", ["phone_hash"])
        op.create_index("ix_registration_challenge_created", "customer_registration_challenges", ["created_at"])
        op.create_index("ix_registration_challenge_expires", "customer_registration_challenges", ["expires_at"])
    if not inspector.has_table("customer_registration_identity_claims"):
        op.create_table(
            "customer_registration_identity_claims",
            sa.Column("business_id", sa.Integer(), primary_key=True),
            sa.Column("identity_type", sa.String(10), primary_key=True),
            sa.Column("identity_hash", sa.String(64), primary_key=True),
            sa.Column("customer_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="SET NULL"),
        )
        op.create_index("ix_registration_claim_customer", "customer_registration_identity_claims", ["customer_id"])


def downgrade():
    raise RuntimeError("Downgrade bloqueado: eliminar verificaciones y consentimientos destruiría evidencia de privacidad.")
