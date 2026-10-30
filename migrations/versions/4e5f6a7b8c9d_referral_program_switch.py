"""add referral program switch and append-only audit

Revision ID: 4e5f6a7b8c9d
Revises: 3d4e5f6a7b8c
"""

from alembic import op
import sqlalchemy as sa


revision = "4e5f6a7b8c9d"
down_revision = "3d4e5f6a7b8c"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    op.add_column("businesses", sa.Column(
        "referrals_enabled", sa.Boolean(), nullable=False,
        server_default=sa.false(),
    ))
    op.execute("""
        UPDATE businesses
        SET referrals_enabled = TRUE
        WHERE EXISTS (
            SELECT 1 FROM referral_participants rp
            WHERE rp.business_id = businesses.id
              AND rp.is_enabled = TRUE
              AND rp.revoked_at IS NULL
        )
    """)
    op.create_table(
        "referral_program_audits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("actor_type", sa.String(30), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("previous_enabled", sa.Boolean(), nullable=False),
        sa.Column("new_enabled", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["business_id"], ["businesses.id"], ondelete="RESTRICT",
            name="fk_referral_program_audit_business",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_id", "request_id",
            name="uq_referral_program_audit_request",
        ),
    )
    op.create_index(
        "ix_referral_program_audits_business_id",
        "referral_program_audits", ["business_id"],
    )
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE referral_program_audits ENABLE ROW LEVEL SECURITY")
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_referral_program_audit_mutation()
            RETURNS trigger AS $$ BEGIN
                RAISE EXCEPTION 'referral program audit is append-only';
            END; $$ LANGUAGE plpgsql SET search_path = pg_catalog, public
        """)
        op.execute("""
            CREATE TRIGGER trg_referral_program_audits_append_only
            BEFORE UPDATE OR DELETE ON referral_program_audits
            FOR EACH ROW EXECUTE FUNCTION prevent_referral_program_audit_mutation()
        """)
    else:
        for action in ("UPDATE", "DELETE"):
            op.execute(f"""
                CREATE TRIGGER trg_referral_program_audits_no_{action.lower()}
                BEFORE {action} ON referral_program_audits
                BEGIN SELECT RAISE(ABORT, 'referral program audit is append-only'); END
            """)


def downgrade():
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM referral_program_audits")).scalar_one():
        raise RuntimeError("Downgrade bloqueado: existe auditoría del programa de referidos.")
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_referral_program_audits_append_only ON referral_program_audits")
        op.execute("DROP FUNCTION IF EXISTS prevent_referral_program_audit_mutation()")
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_referral_program_audits_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_referral_program_audits_no_delete")
    op.drop_table("referral_program_audits")
    op.drop_column("businesses", "referrals_enabled")
