"""add bounded case-insensitive staff email lookup index

Revision ID: 0a1b2c3d4e5f
Revises: f9a0b1c2d3e4
"""
from alembic import op


revision = "0a1b2c3d4e5f"
down_revision = "f9a0b1c2d3e4"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("CREATE INDEX IF NOT EXISTS ix_staff_email_lower ON staff (lower(email))")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_staff_email_lower")
