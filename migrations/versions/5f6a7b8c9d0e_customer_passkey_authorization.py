"""customer passkey authorization and assisted linking

Revision ID: 5f6a7b8c9d0e
Revises: 4e5f6a7b8c9d
"""

from alembic import op
import sqlalchemy as sa


revision = "5f6a7b8c9d0e"
down_revision = "4e5f6a7b8c9d"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "customer_linking_enrollments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("business_id", sa.Integer, nullable=False),
        sa.Column("customer_id", sa.Integer, nullable=False),
        sa.Column("authorized_by_staff_id", sa.Integer, nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("browser_binding_hash", sa.String(64), nullable=True, unique=True),
        sa.Column("pairing_code_hash", sa.String(64), nullable=True),
        sa.Column("pairing_failed_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pairing_locked_at", sa.DateTime, nullable=True),
        sa.Column("webauthn_challenge", sa.LargeBinary, nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("recovery_mode", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("human_check_note", sa.String(500), nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("claimed_at", sa.DateTime, nullable=True),
        sa.Column("approved_at", sa.DateTime, nullable=True),
        sa.Column("consumed_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.CheckConstraint("status IN ('pending','claimed','approved','registration_pending','verifying','completed','cancelled','expired')", name="ck_customer_linking_enrollment_status"),
        sa.ForeignKeyConstraint(["business_id", "customer_id"], ["customers.business_id", "customers.id"], ondelete="RESTRICT", name="fk_customer_linking_customer_tenant"),
        sa.ForeignKeyConstraint(["business_id", "authorized_by_staff_id"], ["staff.business_id", "staff.id"], ondelete="RESTRICT", name="fk_customer_linking_staff_tenant"),
        sa.UniqueConstraint("business_id", "id", name="uq_customer_linking_business_id_id"),
    )
    op.create_index("ix_customer_linking_enrollments_business_id", "customer_linking_enrollments", ["business_id"])
    op.create_index("ix_customer_linking_enrollments_customer_id", "customer_linking_enrollments", ["customer_id"])
    op.create_index("ix_customer_linking_enrollments_expires_at", "customer_linking_enrollments", ["expires_at"])
    op.create_index("ix_customer_linking_enrollments_status", "customer_linking_enrollments", ["status"])

    op.create_table(
        "customer_credentials",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("business_id", sa.Integer, nullable=False),
        sa.Column("customer_id", sa.Integer, nullable=False),
        sa.Column("credential_id", sa.LargeBinary, nullable=False, unique=True),
        sa.Column("public_key", sa.LargeBinary, nullable=False),
        sa.Column("sign_count", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("device_type", sa.String(30), nullable=True),
        sa.Column("backed_up", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("transports_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("last_used_at", sa.DateTime, nullable=True),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.Column("revoked_reason", sa.String(500), nullable=True),
        sa.ForeignKeyConstraint(["business_id", "customer_id"], ["customers.business_id", "customers.id"], ondelete="RESTRICT", name="fk_customer_credential_customer_tenant"),
        sa.UniqueConstraint("business_id", "id", name="uq_customer_credential_business_id_id"),
    )
    op.create_index("ix_customer_credentials_business_id", "customer_credentials", ["business_id"])
    op.create_index("ix_customer_credentials_customer_id", "customer_credentials", ["customer_id"])
    op.create_index("ix_customer_credentials_is_active", "customer_credentials", ["is_active"])

    op.create_table(
        "customer_sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("business_id", sa.Integer, nullable=False),
        sa.Column("customer_id", sa.Integer, nullable=False),
        sa.Column("credential_id", sa.Integer, nullable=False),
        sa.Column("verifier_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("last_active_at", sa.DateTime, nullable=False),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.ForeignKeyConstraint(["business_id", "customer_id"], ["customers.business_id", "customers.id"], ondelete="RESTRICT", name="fk_customer_session_customer_tenant"),
        sa.ForeignKeyConstraint(["business_id", "credential_id"], ["customer_credentials.business_id", "customer_credentials.id"], ondelete="RESTRICT", name="fk_customer_session_credential_tenant"),
    )
    op.create_index("ix_customer_sessions_business_id", "customer_sessions", ["business_id"])
    op.create_index("ix_customer_sessions_customer_id", "customer_sessions", ["customer_id"])
    op.create_index("ix_customer_sessions_expires_at", "customer_sessions", ["expires_at"])

    op.create_table(
        "customer_auth_challenges",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("business_id", sa.Integer, nullable=False),
        sa.Column("browser_binding_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("challenge", sa.LargeBinary, nullable=False),
        sa.Column("expires_at", sa.DateTime, nullable=False),
        sa.Column("consumed_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="RESTRICT"),
    )
    op.create_index("ix_customer_auth_challenges_business_id", "customer_auth_challenges", ["business_id"])
    op.create_index("ix_customer_auth_challenges_expires_at", "customer_auth_challenges", ["expires_at"])

    op.create_table(
        "customer_auth_audits",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("business_id", sa.Integer, nullable=False),
        sa.Column("customer_id", sa.Integer, nullable=False),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("actor_type", sa.String(30), nullable=False),
        sa.Column("actor_id", sa.Integer, nullable=True),
        sa.Column("enrollment_id", sa.String(64), nullable=True),
        sa.Column("credential_id", sa.Integer, nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.ForeignKeyConstraint(["business_id", "customer_id"], ["customers.business_id", "customers.id"], ondelete="RESTRICT", name="fk_customer_auth_audit_customer_tenant"),
        sa.ForeignKeyConstraint(["business_id", "enrollment_id"], ["customer_linking_enrollments.business_id", "customer_linking_enrollments.id"], ondelete="RESTRICT", name="fk_customer_auth_audit_enrollment_tenant"),
        sa.ForeignKeyConstraint(["business_id", "credential_id"], ["customer_credentials.business_id", "customer_credentials.id"], ondelete="RESTRICT", name="fk_customer_auth_audit_credential_tenant"),
    )
    op.create_index("ix_customer_auth_audits_business_id", "customer_auth_audits", ["business_id"])
    op.create_index("ix_customer_auth_audits_customer_id", "customer_auth_audits", ["customer_id"])

    bind = op.get_bind()
    protected = ("customer_linking_enrollments", "customer_credentials", "customer_sessions", "customer_auth_challenges", "customer_auth_audits")
    if bind.dialect.name == "postgresql":
        for table in protected:
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_customer_auth_audit_mutation()
            RETURNS trigger AS $$ BEGIN RAISE EXCEPTION 'customer auth audit is append-only'; END;
            $$ LANGUAGE plpgsql SET search_path = pg_catalog, public
        """)
        op.execute("CREATE TRIGGER trg_customer_auth_audits_append_only BEFORE UPDATE OR DELETE ON customer_auth_audits FOR EACH ROW EXECUTE FUNCTION prevent_customer_auth_audit_mutation()")
    else:
        for action in ("UPDATE", "DELETE"):
            op.execute(f"CREATE TRIGGER trg_customer_auth_audits_no_{action.lower()} BEFORE {action} ON customer_auth_audits BEGIN SELECT RAISE(ABORT, 'customer auth audit is append-only'); END")


def downgrade():
    bind = op.get_bind()
    for table in ("customer_auth_audits", "customer_sessions", "customer_credentials", "customer_linking_enrollments", "customer_auth_challenges"):
        if bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one():
            raise RuntimeError("Downgrade bloqueado: existe historial de autorización de clientes.")
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_customer_auth_audits_append_only ON customer_auth_audits")
        op.execute("DROP FUNCTION IF EXISTS prevent_customer_auth_audit_mutation()")
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_customer_auth_audits_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_customer_auth_audits_no_delete")
    for table in ("customer_auth_audits", "customer_auth_challenges", "customer_sessions", "customer_credentials", "customer_linking_enrollments"):
        op.drop_table(table)
