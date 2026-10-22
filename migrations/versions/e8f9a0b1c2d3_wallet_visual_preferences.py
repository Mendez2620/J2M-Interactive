"""Add optional Wallet visual preferences.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
"""

from alembic import op
import sqlalchemy as sa


revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("businesses") as batch_op:
        batch_op.add_column(sa.Column("wallet_program_name", sa.String(length=60), nullable=True))
        batch_op.add_column(sa.Column("wallet_card_text", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("wallet_text_color", sa.String(length=7), nullable=True))
        batch_op.add_column(sa.Column("wallet_balance_label", sa.String(length=30), nullable=True))
        batch_op.add_column(sa.Column("wallet_instructions", sa.String(length=500), nullable=True))
        batch_op.add_column(sa.Column("wallet_contact", sa.String(length=160), nullable=True))
        batch_op.add_column(sa.Column("wallet_preset", sa.String(length=20), nullable=False, server_default="classic"))


def downgrade():
    raise RuntimeError(
        "Downgrade bloqueado: eliminar la configuración Wallet podría destruir personalización existente."
    )
