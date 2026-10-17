"""add controlled business visual preferences

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
"""
from alembic import op
import sqlalchemy as sa

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None

def upgrade():
    with op.batch_alter_table("businesses") as batch:
        batch.add_column(sa.Column("visual_theme", sa.String(20), nullable=False, server_default="modern"))
        batch.add_column(sa.Column("font_style", sa.String(20), nullable=False, server_default="modern"))
        batch.add_column(sa.Column("text_size", sa.String(20), nullable=False, server_default="standard"))

def downgrade():
    raise RuntimeError("Downgrade bloqueado: eliminar preferencias visuales no es una reversión segura.")
