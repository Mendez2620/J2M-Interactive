"""controlled Google Wallet publications

Revision ID: 8c9d0e1f2a3b
Revises: 7b8c9d0e1f2a
"""

from alembic import op
import sqlalchemy as sa


revision = "8c9d0e1f2a3b"
down_revision = "7b8c9d0e1f2a"
branch_labels = None
depends_on = None


PUBLICATION_STATUSES = "'pending','processing','completed','failed','superseded','cancelled'"
JOB_STATUSES = "'pending','processing','completed','failed','skipped','superseded','cancelled'"


def upgrade():
    with op.batch_alter_table("businesses") as batch:
        batch.add_column(sa.Column("wallet_design_version", sa.Integer(), nullable=False, server_default="1"))
        batch.create_check_constraint("ck_business_wallet_design_version_positive", "wallet_design_version > 0")
    op.create_table(
        "google_wallet_publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("design_version", sa.Integer(), nullable=False),
        sa.Column("execution_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active_key", sa.String(20), nullable=True),
        sa.Column("provider_mode", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_by_admin_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("total_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("class_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "business_id", "design_version", "execution_number",
            name="uq_google_wallet_publication_execution",
        ),
        sa.UniqueConstraint("business_id", "active_key", name="uq_google_wallet_publication_active"),
        sa.UniqueConstraint("business_id", "id", name="uq_google_wallet_publication_business_id_id"),
        sa.CheckConstraint(f"status IN ({PUBLICATION_STATUSES})", name="ck_google_wallet_publication_status"),
        sa.CheckConstraint("provider_mode IN ('mock','real')", name="ck_google_wallet_publication_mode"),
        sa.CheckConstraint(
            "design_version > 0 AND execution_number > 0 AND total_jobs >= 0 AND attempts >= 0",
            name="ck_google_wallet_publication_counts",
        ),
        sa.CheckConstraint(
            "(status IN ('pending','processing') AND active_key = 'active') OR "
            "(status NOT IN ('pending','processing') AND active_key IS NULL)",
            name="ck_google_wallet_publication_active_state",
        ),
    )
    op.create_index("ix_google_wallet_publications_business_id", "google_wallet_publications", ["business_id"])
    op.create_index("ix_google_wallet_publications_status", "google_wallet_publications", ["status"])
    op.create_index("ix_google_wallet_publications_next_attempt_at", "google_wallet_publications", ["next_attempt_at"])
    op.create_table(
        "google_wallet_publication_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("publication_id", sa.Integer(), nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("design_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["business_id", "publication_id"],
            ["google_wallet_publications.business_id", "google_wallet_publications.id"],
            ondelete="RESTRICT", name="fk_google_wallet_publication_job_batch_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            ondelete="RESTRICT", name="fk_google_wallet_publication_job_customer_tenant",
        ),
        sa.UniqueConstraint("publication_id", "customer_id", name="uq_google_wallet_publication_job_customer"),
        sa.CheckConstraint(f"status IN ({JOB_STATUSES})", name="ck_google_wallet_publication_job_status"),
        sa.CheckConstraint("design_version > 0 AND attempts >= 0", name="ck_google_wallet_publication_job_counts"),
    )
    for name, columns in (
        ("ix_google_wallet_publication_jobs_publication_id", ["publication_id"]),
        ("ix_google_wallet_publication_jobs_business_id", ["business_id"]),
        ("ix_google_wallet_publication_jobs_customer_id", ["customer_id"]),
        ("ix_google_wallet_publication_jobs_status", ["status"]),
        ("ix_google_wallet_publication_jobs_next_attempt_at", ["next_attempt_at"]),
    ):
        op.create_index(name, "google_wallet_publication_jobs", columns)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE google_wallet_publications ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE google_wallet_publication_jobs ENABLE ROW LEVEL SECURITY")
        op.execute("REVOKE ALL ON TABLE google_wallet_publications, google_wallet_publication_jobs FROM PUBLIC")
        op.execute("REVOKE ALL ON SEQUENCE google_wallet_publications_id_seq, google_wallet_publication_jobs_id_seq FROM PUBLIC")
        op.execute("""
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON TABLE google_wallet_publications, google_wallet_publication_jobs FROM anon;
                REVOKE ALL ON SEQUENCE google_wallet_publications_id_seq, google_wallet_publication_jobs_id_seq FROM anon;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON TABLE google_wallet_publications, google_wallet_publication_jobs FROM authenticated;
                REVOKE ALL ON SEQUENCE google_wallet_publications_id_seq, google_wallet_publication_jobs_id_seq FROM authenticated;
              END IF;
            END $$
        """)


def downgrade():
    count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM google_wallet_publications")).scalar_one()
    if count:
        raise RuntimeError("Downgrade bloqueado: existe historial de publicaciones Google Wallet.")
    op.drop_table("google_wallet_publication_jobs")
    op.drop_table("google_wallet_publications")
    with op.batch_alter_table("businesses") as batch:
        batch.drop_constraint("ck_business_wallet_design_version_positive", type_="check")
        batch.drop_column("wallet_design_version")
