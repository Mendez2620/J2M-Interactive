"""add tenant promoters and verified referral attribution

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
"""
from alembic import op
import sqlalchemy as sa


revision = "f9a0b1c2d3e4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("promoters"):
        op.create_table(
        "promoters",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("username", sa.String(80), nullable=False),
        sa.Column("email", sa.String(120), nullable=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("referral_code", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auth_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("referral_code"),
        sa.UniqueConstraint("business_id", "username", name="uq_promoter_business_username"),
        )
        op.create_index("ix_promoters_business_id", "promoters", ["business_id"])
    if not inspector.has_table("promoter_sessions"):
        op.create_table(
        "promoter_sessions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("promoter_id", sa.Integer(), nullable=False),
        sa.Column("verifier_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["promoter_id"], ["promoters.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("verifier_hash"),
        )
        op.create_index("ix_promoter_sessions_promoter_id", "promoter_sessions", ["promoter_id"])
    challenge_columns = {column["name"] for column in inspector.get_columns("customer_registration_challenges")}
    if "promoter_id" not in challenge_columns:
        with op.batch_alter_table("customer_registration_challenges") as batch:
            batch.add_column(sa.Column("promoter_id", sa.Integer(), nullable=True))
            batch.create_foreign_key("fk_registration_challenge_promoter", "promoters", ["promoter_id"], ["id"], ondelete="SET NULL")
            batch.create_index("ix_registration_challenge_promoter_id", ["promoter_id"])
    if not inspector.has_table("referral_attributions"):
        op.create_table(
        "referral_attributions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("promoter_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("business_id > 0", name="ck_referral_business_positive"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["promoter_id"], ["promoters.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("customer_id"),
        sa.UniqueConstraint("business_id", "customer_id", name="uq_referral_business_customer"),
        )
        op.create_index("ix_referral_attributions_business_id", "referral_attributions", ["business_id"])
        op.create_index("ix_referral_attributions_promoter_id", "referral_attributions", ["promoter_id"])
        op.create_index("ix_referral_attributions_created_at", "referral_attributions", ["created_at"])


def downgrade():
    raise RuntimeError("Downgrade bloqueado: eliminar promotores o atribuciones destruiría historial verificable.")
