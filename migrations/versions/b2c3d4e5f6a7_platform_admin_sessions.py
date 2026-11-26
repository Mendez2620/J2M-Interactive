"""Add individually revocable platform administrator sessions."""

from alembic import op
import sqlalchemy as sa

revision = "b2c3d4e5f6a7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("platform_admin_sessions"):
        op.create_table(
            "platform_admin_sessions",
            sa.Column("token_digest", sa.String(64), primary_key=True),
            sa.Column("admin_id", sa.Integer, sa.ForeignKey("platform_admins.id", ondelete="CASCADE"), nullable=False),
            sa.Column("password_digest", sa.String(64), nullable=False),
            sa.Column("expires_at", sa.DateTime, nullable=False),
            sa.Column("revoked_at", sa.DateTime),
        )
        op.create_index("ix_platform_admin_sessions_admin_id", "platform_admin_sessions", ["admin_id"])
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE platform_admin_sessions ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON platform_admin_sessions FROM PUBLIC")
        for role in ("anon", "authenticated"):
            op.execute(
                f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
                f"THEN REVOKE ALL ON platform_admin_sessions FROM {role}; END IF; END $$"
            )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM platform_admin_sessions LIMIT 1")).scalar():
        raise RuntimeError("Existing administrator sessions prevent downgrade")
    op.drop_table("platform_admin_sessions")
