"""security access channels, revocable sessions and idempotency

Revision ID: c4f1a2b3d4e5
Revises: 9a85c3791398
"""
from alembic import op
import sqlalchemy as sa


revision = "c4f1a2b3d4e5"
down_revision = "9a85c3791398"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    business_columns = {c["name"] for c in inspector.get_columns("businesses")}
    with op.batch_alter_table("businesses") as batch:
        if "web_access_enabled" not in business_columns:
            batch.add_column(sa.Column("web_access_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "mobile_access_enabled" not in business_columns:
            batch.add_column(sa.Column("mobile_access_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))

    staff_columns = {c["name"] for c in inspector.get_columns("staff")}
    with op.batch_alter_table("staff") as batch:
        if "web_access_enabled" not in staff_columns:
            batch.add_column(sa.Column("web_access_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "mobile_access_enabled" not in staff_columns:
            batch.add_column(sa.Column("mobile_access_enabled", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "auth_version" not in staff_columns:
            batch.add_column(sa.Column("auth_version", sa.Integer(), nullable=False, server_default="0"))
        if "permissions" not in staff_columns:
            batch.add_column(sa.Column("permissions", sa.String(length=500), nullable=False, server_default=""))

    op.execute("UPDATE staff SET permissions = 'view_stats' WHERE role = 'manager' AND permissions = ''")
    op.execute("UPDATE staff SET permissions = 'scanner_operate' WHERE role = 'cashier' AND permissions = ''")

    transaction_columns = {c["name"] for c in inspector.get_columns("transactions")}
    with op.batch_alter_table("transactions") as batch:
        if "request_id" not in transaction_columns:
            batch.add_column(sa.Column("request_id", sa.String(length=64), nullable=True))
            batch.create_index("ix_transactions_request_id", ["request_id"], unique=True)

    if "staff_sessions" not in set(inspector.get_table_names()):
        op.create_table(
            "staff_sessions",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("staff_id", sa.Integer(), nullable=False),
            sa.Column("channel", sa.String(length=10), nullable=False),
            sa.Column("refresh_jti_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["staff_id"], ["staff.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("refresh_jti_hash"),
        )
        op.create_index("ix_staff_sessions_staff_id", "staff_sessions", ["staff_id"], unique=False)


def downgrade():
    raise RuntimeError(
        "Downgrade bloqueado para preservar sesiones, configuración de accesos e identificadores de idempotencia. "
        "Restaura un respaldo verificado si necesitas volver a una versión anterior."
    )
