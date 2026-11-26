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


def downgrade():
    if op.get_bind().execute(sa.text(
        "SELECT 1 FROM google_wallet_sync_jobs WHERE status = 'processing' LIMIT 1"
    )).scalar():
        raise RuntimeError("Drain Google Wallet sync claims before downgrading")
    op.drop_column("google_wallet_sync_jobs", "claim_token")
