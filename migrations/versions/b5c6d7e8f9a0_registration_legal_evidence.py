"""add immutable legal evidence to pending registrations

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
"""
from alembic import op
import sqlalchemy as sa

revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    customer_columns = {column["name"] for column in inspector.get_columns("customers")}
    if "terms_content_hash" not in customer_columns:
        op.add_column("customers", sa.Column("terms_content_hash", sa.String(64), nullable=True))
    if "privacy_content_hash" not in customer_columns:
        op.add_column("customers", sa.Column("privacy_content_hash", sa.String(64), nullable=True))

    challenge_columns = {
        column["name"] for column in inspector.get_columns("customer_registration_challenges")
    }
    additions = (
        ("terms_accepted_at", sa.DateTime()),
        ("terms_content_hash", sa.String(64)),
        ("privacy_accepted_at", sa.DateTime()),
        ("privacy_content_hash", sa.String(64)),
        ("marketing_consent_at", sa.DateTime()),
    )
    for name, type_ in additions:
        if name not in challenge_columns:
            op.add_column("customer_registration_challenges", sa.Column(name, type_, nullable=True))
    # Old pending challenges lack immutable document hashes. Invalidate them
    # instead of inventing legal evidence; users can safely request a new code.
    op.execute(sa.text(
        "UPDATE customer_registration_challenges "
        "SET consumed_at = CURRENT_TIMESTAMP WHERE consumed_at IS NULL"
    ))


def downgrade():
    raise RuntimeError("Downgrade bloqueado: eliminar evidencia legal impediría auditar consentimientos.")
