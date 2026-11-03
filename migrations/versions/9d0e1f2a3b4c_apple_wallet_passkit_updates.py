"""secure Apple Wallet PassKit updates and APNs outbox

Revision ID: 9d0e1f2a3b4c
Revises: 8c9d0e1f2a3b
"""

from alembic import op
import sqlalchemy as sa


revision = "9d0e1f2a3b4c"
down_revision = "8c9d0e1f2a3b"
branch_labels = None
depends_on = None


def upgrade():
    existing_device_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("apple_devices")}
    for column in (
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_notified_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("invalidated_at", sa.DateTime(), nullable=True),
    ):
        if column.name not in existing_device_columns:
            op.add_column("apple_devices", column)
    op.create_index("uq_apple_device_customer_id_id", "apple_devices", ["customer_id", "id"], unique=True)
    op.create_table(
        "apple_pass_update_sequence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("value", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.execute(sa.text("INSERT INTO apple_pass_update_sequence (id, value) VALUES (1, 0)"))
    op.create_table(
        "apple_pass_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("pass_type_identifier", sa.String(120), nullable=False),
        sa.Column("serial_number", sa.String(120), nullable=False),
        sa.Column("auth_key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("auth_token_digest", sa.String(64), nullable=False),
        sa.Column("update_version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("update_tag", sa.BigInteger(), nullable=False),
        sa.Column("design_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("last_served_at", sa.DateTime(), nullable=True),
        sa.Column("last_served_update_tag", sa.BigInteger(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["business_id", "customer_id"], ["customers.business_id", "customers.id"],
                                ondelete="RESTRICT", name="fk_apple_pass_credential_customer_tenant"),
        sa.UniqueConstraint("business_id", "customer_id", name="uq_apple_pass_credential_customer"),
        sa.UniqueConstraint("pass_type_identifier", "serial_number", name="uq_apple_pass_identity"),
        sa.CheckConstraint("update_version >= 1 AND update_tag >= 1 AND auth_key_version >= 1", name="ck_apple_pass_versions_positive"),
    )
    op.create_index("ix_apple_pass_credentials_business_id", "apple_pass_credentials", ["business_id"])
    op.create_index("ix_apple_pass_credentials_customer_id", "apple_pass_credentials", ["customer_id"])
    op.create_index("ix_apple_pass_credentials_serial_number", "apple_pass_credentials", ["serial_number"])
    op.create_index("ix_apple_pass_credentials_update_tag", "apple_pass_credentials", ["update_tag"])
    op.create_table(
        "apple_wallet_publications",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("design_version", sa.Integer(), nullable=False),
        sa.Column("execution_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("active_key", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_by_admin_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(255), nullable=False),
        sa.Column("total_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("business_id", "design_version", "execution_number", name="uq_apple_wallet_publication_execution"),
        sa.UniqueConstraint("business_id", "active_key", name="uq_apple_wallet_publication_active"),
        sa.UniqueConstraint("business_id", "id", name="uq_apple_wallet_publication_business_id_id"),
        sa.CheckConstraint("status IN ('pending','processing','completed','failed','superseded','cancelled')", name="ck_apple_wallet_publication_status"),
        sa.CheckConstraint("(status IN ('pending','processing') AND active_key = 'active') OR (status NOT IN ('pending','processing') AND active_key IS NULL)", name="ck_apple_wallet_publication_active_state"),
    )
    op.create_index("ix_apple_wallet_publications_business_id", "apple_wallet_publications", ["business_id"])
    op.create_index("ix_apple_wallet_publications_status", "apple_wallet_publications", ["status"])
    op.create_table(
        "apple_wallet_push_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("publication_id", sa.Integer(), nullable=True),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("device_id", sa.Integer(), nullable=False),
        sa.Column("design_version", sa.Integer(), nullable=False),
        sa.Column("target_update_version", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["business_id", "customer_id"], ["customers.business_id", "customers.id"], ondelete="RESTRICT", name="fk_apple_wallet_push_customer_tenant"),
        sa.ForeignKeyConstraint(["business_id", "publication_id"], ["apple_wallet_publications.business_id", "apple_wallet_publications.id"], ondelete="RESTRICT", name="fk_apple_wallet_push_publication_tenant"),
        sa.ForeignKeyConstraint(["customer_id", "device_id"], ["apple_devices.customer_id", "apple_devices.id"], ondelete="RESTRICT", name="fk_apple_wallet_push_device_customer"),
        sa.UniqueConstraint("publication_id", "device_id", name="uq_apple_wallet_publication_device"),
        sa.CheckConstraint("status IN ('pending','processing','completed','failed','superseded','cancelled')", name="ck_apple_wallet_push_status"),
        sa.CheckConstraint("attempts >= 0 AND target_update_version >= 1", name="ck_apple_wallet_push_counts"),
    )
    for column in ("publication_id", "business_id", "customer_id", "device_id", "status", "next_attempt_at"):
        op.create_index(f"ix_apple_wallet_push_jobs_{column}", "apple_wallet_push_jobs", [column])
    if op.get_bind().dialect.name == "postgresql":
        tables = "apple_pass_update_sequence, apple_pass_credentials, apple_wallet_publications, apple_wallet_push_jobs"
        for table in tables.split(", "):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE {tables} FROM PUBLIC")
        op.execute("REVOKE ALL ON SEQUENCE apple_pass_credentials_id_seq, apple_wallet_publications_id_seq, apple_wallet_push_jobs_id_seq FROM PUBLIC")
        op.execute("""
            DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='anon') THEN
                REVOKE ALL ON TABLE apple_pass_update_sequence, apple_pass_credentials, apple_wallet_publications, apple_wallet_push_jobs FROM anon;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='authenticated') THEN
                REVOKE ALL ON TABLE apple_pass_update_sequence, apple_pass_credentials, apple_wallet_publications, apple_wallet_push_jobs FROM authenticated;
              END IF;
            END $$
        """)


def downgrade():
    connection = op.get_bind()
    for table in ("apple_wallet_push_jobs", "apple_wallet_publications", "apple_pass_credentials"):
        if connection.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar():
            raise RuntimeError("Downgrade blocked: Apple Wallet update history must be preserved.")
    op.drop_table("apple_wallet_push_jobs")
    op.drop_table("apple_wallet_publications")
    op.drop_table("apple_pass_credentials")
    op.drop_table("apple_pass_update_sequence")
    op.drop_index("uq_apple_device_customer_id_id", table_name="apple_devices")
    with op.batch_alter_table("apple_devices") as batch:
        batch.drop_column("invalidated_at")
        batch.drop_column("last_notified_version")
        batch.drop_column("is_active")
