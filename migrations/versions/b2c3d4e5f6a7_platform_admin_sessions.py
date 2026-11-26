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
    if not sa.inspect(bind).has_table("admin_wallet_reviews"):
        op.create_table(
            "admin_wallet_reviews",
            sa.Column("id", sa.String(32), primary_key=True),
            sa.Column("session_digest", sa.String(64), sa.ForeignKey("platform_admin_sessions.token_digest"), nullable=False),
            sa.Column("business_id", sa.Integer, sa.ForeignKey("businesses.id"), nullable=False),
            sa.Column("candidate_ids", sa.JSON, nullable=False),
            sa.Column("snapshot", sa.JSON, nullable=False),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("expires_at", sa.DateTime, nullable=False),
            sa.Column("state", sa.String(16), nullable=False, server_default="prepared"),
            sa.Column("confirmed_at", sa.DateTime),
            sa.Column("claimed_at", sa.DateTime),
            sa.Column("owner", sa.String(32)),
            sa.Column("finished_at", sa.DateTime),
            sa.Column("result", sa.JSON),
            sa.CheckConstraint("state IN ('prepared','queued','running','finished','expired','rejected','inconclusive')",
                               name="ck_admin_wallet_review_state"),
            sa.CheckConstraint(
                "(state NOT IN ('queued', 'running', 'finished', 'inconclusive') OR confirmed_at IS NOT NULL) AND (state != 'queued' OR (owner IS NULL AND claimed_at IS NULL AND finished_at IS NULL)) AND (state NOT IN ('running', 'finished', 'inconclusive') OR (owner IS NOT NULL AND claimed_at IS NOT NULL)) AND (state NOT IN ('finished', 'inconclusive') OR finished_at IS NOT NULL)",
                name="ck_admin_wallet_review_claim",
            ),
        )
        op.create_index("ix_admin_wallet_reviews_business_id", "admin_wallet_reviews", ["business_id"])
        op.create_index("ix_admin_wallet_review_queue", "admin_wallet_reviews", ["state", "created_at"])
    inspector = sa.inspect(bind)
    for name, primary, required, target, indexes in (
        ("platform_admin_sessions", "token_digest",
         {"token_digest", "admin_id", "password_digest", "expires_at", "revoked_at"},
         {("admin_id", "platform_admins")}, {"ix_platform_admin_sessions_admin_id"}),
        ("admin_wallet_reviews", "id",
         {"id", "session_digest", "business_id", "candidate_ids", "snapshot", "fingerprint",
          "created_at", "expires_at", "state", "confirmed_at", "claimed_at", "owner", "finished_at", "result"},
         {("session_digest", "platform_admin_sessions"), ("business_id", "businesses")},
         {"ix_admin_wallet_reviews_business_id", "ix_admin_wallet_review_queue"}),
    ):
        foreign = {(item["constrained_columns"][0], item["referred_table"])
                   for item in inspector.get_foreign_keys(name) if len(item["constrained_columns"]) == 1}
        if ({item["name"] for item in inspector.get_columns(name)} != required
                or inspector.get_pk_constraint(name)["constrained_columns"] != [primary]
                or not target <= foreign
                or not indexes <= {item["name"] for item in inspector.get_indexes(name)}):
            raise RuntimeError(f"Existing {name} schema is incomplete")
    if not {"ck_admin_wallet_review_state", "ck_admin_wallet_review_claim"} <= {
        item["name"] for item in inspector.get_check_constraints("admin_wallet_reviews")
    }:
        raise RuntimeError("Existing reconciliation review state constraint is missing")
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE platform_admin_sessions ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON platform_admin_sessions FROM PUBLIC")
        op.execute("ALTER TABLE admin_wallet_reviews ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON admin_wallet_reviews FROM PUBLIC")
        for role in ("anon", "authenticated"):
            op.execute(
                f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
                f"THEN REVOKE ALL ON platform_admin_sessions, admin_wallet_reviews FROM {role}; END IF; END $$"
            )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM admin_wallet_reviews LIMIT 1")).scalar():
        raise RuntimeError("Existing reconciliation reviews prevent downgrade")
    if op.get_bind().execute(sa.text("SELECT 1 FROM platform_admin_sessions LIMIT 1")).scalar():
        raise RuntimeError("Existing administrator sessions prevent downgrade")
    op.drop_table("admin_wallet_reviews")
    op.drop_table("platform_admin_sessions")
