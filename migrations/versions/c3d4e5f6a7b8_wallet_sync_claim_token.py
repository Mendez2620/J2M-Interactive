"""Fence ordinary Google Wallet outbox claims with an opaque token."""

from alembic import op
import sqlalchemy as sa

revision = "c3d4e5f6a7b8"
down_revision = "b2c3d4e5f6a7"
branch_labels = None
depends_on = None


def upgrade():
    columns = {item["name"]: item for item in sa.inspect(op.get_bind()).get_columns("google_wallet_sync_jobs")}
    if "claim_token" not in columns:
        op.add_column("google_wallet_sync_jobs", sa.Column("claim_token", sa.String(32), nullable=True))
    elif (not isinstance(columns["claim_token"]["type"], sa.String)
          or columns["claim_token"]["type"].length != 32
          or not columns["claim_token"]["nullable"]):
        raise RuntimeError("Existing Wallet claim token column is incompatible")
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("google_wallet_referral_jobs"):
        op.create_table(
            "google_wallet_referral_jobs",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("business_id", sa.Integer, sa.ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False),
            sa.Column("customer_id", sa.Integer),
            sa.Column("cursor", sa.Integer, nullable=False, server_default="0"),
            sa.Column("scheduled_expiry", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
            sa.Column("token", sa.String(32)),
            sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
            sa.Column("run_at", sa.DateTime, nullable=False),
            sa.Column("locked_at", sa.DateTime),
            sa.Column("created_at", sa.DateTime, nullable=False),
            sa.Column("updated_at", sa.DateTime, nullable=False),
            sa.Column("outcome", sa.String(24)),
            sa.ForeignKeyConstraint(["business_id", "customer_id"], ["customers.business_id", "customers.id"],
                                    ondelete="CASCADE", name="fk_wallet_referral_customer"),
            sa.CheckConstraint("status IN ('pending','processing','completed','missing','failed')",
                               name="ck_wallet_referral_status"),
        )
        op.create_index("ix_wallet_referral_due", "google_wallet_referral_jobs", ["status", "run_at", "id"])
        op.create_index("ix_wallet_referral_business", "google_wallet_referral_jobs", ["business_id", "status"])
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE google_wallet_referral_jobs ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON google_wallet_referral_jobs FROM PUBLIC")
        for role in ("anon", "authenticated"):
            op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') "
                       f"THEN REVOKE ALL ON google_wallet_referral_jobs FROM {role}; END IF; END $$")


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM google_wallet_referral_jobs LIMIT 1")).scalar():
        raise RuntimeError("Existing Wallet referral events prevent downgrade")
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM google_wallet_sync_jobs WHERE status = 'processing' LIMIT 1"
    )).scalar():
        raise RuntimeError("Drain Google Wallet sync claims before downgrading")
    op.drop_table("google_wallet_referral_jobs")
    op.drop_column("google_wallet_sync_jobs", "claim_token")
