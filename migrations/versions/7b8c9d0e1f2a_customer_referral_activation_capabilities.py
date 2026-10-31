"""customer referral activation capabilities

Revision ID: 7b8c9d0e1f2a
Revises: 6a7b8c9d0e1f
"""

from alembic import op
import sqlalchemy as sa


revision = "7b8c9d0e1f2a"
down_revision = "6a7b8c9d0e1f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "customer_referral_activations",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("business_id", sa.Integer, nullable=False),
        sa.Column("participant_id", sa.Integer, nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("consumed_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.ForeignKeyConstraint(
            ["business_id", "participant_id"],
            ["referral_participants.business_id", "referral_participants.id"],
            ondelete="RESTRICT",
            name="fk_customer_referral_activation_participant_tenant",
        ),
        sa.UniqueConstraint("token_hash", name="uq_customer_referral_activation_token_hash"),
        sa.UniqueConstraint(
            "business_id", "id", name="uq_customer_referral_activation_business_id_id",
        ),
    )
    op.create_index(
        "ix_customer_referral_activations_business_id",
        "customer_referral_activations", ["business_id"],
    )
    op.create_index(
        "ix_customer_referral_activations_participant_id",
        "customer_referral_activations", ["participant_id"],
    )
    op.create_index(
        "ix_customer_referral_activations_token_hash",
        "customer_referral_activations", ["token_hash"], unique=True,
    )
    op.create_index(
        "ix_customer_referral_activations_expires_at",
        "customer_referral_activations", ["expires_at"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE customer_referral_activations ENABLE ROW LEVEL SECURITY")


def downgrade():
    count = op.get_bind().execute(sa.text(
        "SELECT COUNT(*) FROM customer_referral_activations"
    )).scalar_one()
    if count:
        raise RuntimeError(
            "Downgrade bloqueado: existen capacidades de activación que no pueden "
            "eliminarse sin perder historial de seguridad."
        )
    op.drop_table("customer_referral_activations")
