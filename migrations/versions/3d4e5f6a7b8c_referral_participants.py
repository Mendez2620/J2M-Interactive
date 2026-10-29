"""add typed staff and customer referral participants

Revision ID: 3d4e5f6a7b8c
Revises: 2c3d4e5f6a7b
"""

from alembic import op
import sqlalchemy as sa


revision = "3d4e5f6a7b8c"
down_revision = "2c3d4e5f6a7b"
branch_labels = None
depends_on = None


def _append_only(bind):
    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_referral_participant_audit_mutation()
            RETURNS trigger AS $$ BEGIN
                RAISE EXCEPTION 'referral participant audit is append-only';
            END; $$ LANGUAGE plpgsql SET search_path = pg_catalog, public
        """)
        op.execute("""
            CREATE TRIGGER trg_referral_participant_audits_append_only
            BEFORE UPDATE OR DELETE ON referral_participant_audits
            FOR EACH ROW EXECUTE FUNCTION prevent_referral_participant_audit_mutation()
        """)
    else:
        for action in ("UPDATE", "DELETE"):
            op.execute(f"""
                CREATE TRIGGER trg_referral_participant_audits_no_{action.lower()}
                BEFORE {action} ON referral_participant_audits
                BEGIN SELECT RAISE(ABORT, 'referral participant audit is append-only'); END
            """)


def _replace_record_guard(bind):
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_referral_incentive_record_subject")
        op.execute("""
            CREATE TRIGGER trg_referral_incentive_record_subject
            BEFORE INSERT ON referral_incentive_records
            WHEN NOT EXISTS (
                SELECT 1 FROM referral_eligibilities e
                JOIN referral_participants p
                  ON p.business_id = e.business_id AND p.id = e.participant_id
                WHERE e.business_id = NEW.business_id
                  AND e.id = NEW.eligibility_id
                  AND e.attribution_id = NEW.attribution_id
                  AND e.source_transaction_id = NEW.source_transaction_id
                  AND p.participant_type = NEW.beneficiary_type
                  AND CASE NEW.beneficiary_type
                        WHEN 'external_promoter' THEN p.promoter_id
                        WHEN 'staff' THEN p.staff_id
                        WHEN 'customer_ambassador' THEN p.customer_id
                      END = NEW.beneficiary_id
            )
            BEGIN SELECT RAISE(ABORT, 'referral incentive subject mismatch'); END
        """)
        return
    if bind.dialect.name != "postgresql":
        return
    op.execute("""
        CREATE OR REPLACE FUNCTION validate_referral_incentive_record_subject()
        RETURNS trigger AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM referral_eligibilities e
                JOIN referral_participants p
                  ON p.business_id = e.business_id AND p.id = e.participant_id
                WHERE e.business_id = NEW.business_id
                  AND e.id = NEW.eligibility_id
                  AND e.attribution_id = NEW.attribution_id
                  AND e.source_transaction_id = NEW.source_transaction_id
                  AND p.participant_type = NEW.beneficiary_type
                  AND CASE NEW.beneficiary_type
                        WHEN 'external_promoter' THEN p.promoter_id
                        WHEN 'staff' THEN p.staff_id
                        WHEN 'customer_ambassador' THEN p.customer_id
                      END = NEW.beneficiary_id
            ) THEN RAISE EXCEPTION 'referral incentive subject mismatch'; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql SET search_path = pg_catalog, public
    """)


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())
    if "referral_participants" in tables:
        op.execute("""
            INSERT INTO referral_participants
                (business_id, participant_type, promoter_id, invitation_code, is_enabled, created_at, updated_at)
            SELECT p.business_id, 'external_promoter', p.id, p.referral_code, TRUE, p.created_at, p.created_at
            FROM promoters p
            WHERE NOT EXISTS (
                SELECT 1 FROM referral_participants rp
                WHERE rp.business_id = p.business_id AND rp.promoter_id = p.id
            )
        """)
        op.execute("""
            UPDATE referral_attributions SET participant_id = (
                SELECT p.id FROM referral_participants p
                WHERE p.business_id = referral_attributions.business_id
                  AND p.promoter_id = referral_attributions.promoter_id
            ) WHERE participant_id IS NULL
        """)
        op.execute("""
            UPDATE referral_eligibilities SET participant_id = (
                SELECT p.id FROM referral_participants p
                WHERE p.business_id = referral_eligibilities.business_id
                  AND p.promoter_id = referral_eligibilities.promoter_id
            ) WHERE participant_id IS NULL
        """)
        if bind.dialect.name == "postgresql":
            op.execute("ALTER TABLE referral_participants ENABLE ROW LEVEL SECURITY")
            op.execute("ALTER TABLE referral_participant_audits ENABLE ROW LEVEL SECURITY")
        _append_only(bind)
        _replace_record_guard(bind)
        return
    if bind.dialect.name == "sqlite":
        op.create_index("uq_staff_business_id_id", "staff", ["business_id", "id"], unique=True)
    else:
        op.create_unique_constraint("uq_staff_business_id_id", "staff", ["business_id", "id"])

    op.create_table(
        "referral_participants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("participant_type", sa.String(30), nullable=False),
        sa.Column("promoter_id", sa.Integer(), nullable=True),
        sa.Column("staff_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("invitation_code", sa.String(48), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("consent_version", sa.String(32), nullable=True),
        sa.Column("consented_at", sa.DateTime(), nullable=True),
        sa.Column("consent_evidence_hash", sa.String(64), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("participant_type IN ('external_promoter','staff','customer_ambassador')", name="ck_referral_participant_type"),
        sa.CheckConstraint(
            "(participant_type = 'external_promoter' AND promoter_id IS NOT NULL AND staff_id IS NULL AND customer_id IS NULL) OR "
            "(participant_type = 'staff' AND promoter_id IS NULL AND staff_id IS NOT NULL AND customer_id IS NULL) OR "
            "(participant_type = 'customer_ambassador' AND promoter_id IS NULL AND staff_id IS NULL AND customer_id IS NOT NULL)",
            name="ck_referral_participant_identity",
        ),
        sa.CheckConstraint(
            "participant_type != 'customer_ambassador' OR (consent_version IS NOT NULL AND consented_at IS NOT NULL AND consent_evidence_hash IS NOT NULL)",
            name="ck_referral_participant_customer_consent",
        ),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="RESTRICT", name="fk_referral_participant_business"),
        sa.ForeignKeyConstraint(["business_id", "promoter_id"], ["promoters.business_id", "promoters.id"], ondelete="RESTRICT", name="fk_referral_participant_promoter_tenant"),
        sa.ForeignKeyConstraint(["business_id", "staff_id"], ["staff.business_id", "staff.id"], ondelete="RESTRICT", name="fk_referral_participant_staff_tenant"),
        sa.ForeignKeyConstraint(["business_id", "customer_id"], ["customers.business_id", "customers.id"], ondelete="RESTRICT", name="fk_referral_participant_customer_tenant"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invitation_code", name="uq_referral_participant_invitation_code"),
        sa.UniqueConstraint("business_id", "id", name="uq_referral_participant_business_id_id"),
        sa.UniqueConstraint("business_id", "promoter_id", name="uq_referral_participant_promoter"),
        sa.UniqueConstraint("business_id", "staff_id", name="uq_referral_participant_staff"),
        sa.UniqueConstraint("business_id", "customer_id", name="uq_referral_participant_customer"),
    )
    op.create_index("ix_referral_participants_business_id", "referral_participants", ["business_id"])
    op.create_index("ix_referral_participants_type", "referral_participants", ["participant_type"])
    op.create_index("ix_referral_participants_enabled", "referral_participants", ["is_enabled"])
    op.execute("""
        INSERT INTO referral_participants
            (business_id, participant_type, promoter_id, invitation_code, is_enabled, created_at, updated_at)
        SELECT business_id, 'external_promoter', id, referral_code, TRUE, created_at, created_at
        FROM promoters
    """)

    with op.batch_alter_table("referral_attributions") as batch:
        batch.add_column(sa.Column("participant_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("referral_eligibilities") as batch:
        batch.add_column(sa.Column("participant_id", sa.Integer(), nullable=True))
    op.execute("""
        UPDATE referral_attributions SET participant_id = (
            SELECT p.id FROM referral_participants p
            WHERE p.business_id = referral_attributions.business_id
              AND p.promoter_id = referral_attributions.promoter_id
        )
    """)
    op.execute("""
        UPDATE referral_eligibilities SET participant_id = (
            SELECT p.id FROM referral_participants p
            WHERE p.business_id = referral_eligibilities.business_id
              AND p.promoter_id = referral_eligibilities.promoter_id
        )
    """)
    with op.batch_alter_table("referral_attributions") as batch:
        batch.alter_column("participant_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("promoter_id", existing_type=sa.Integer(), nullable=True)
        batch.create_foreign_key("fk_referral_attribution_participant_tenant", "referral_participants", ["business_id", "participant_id"], ["business_id", "id"], ondelete="RESTRICT")
        batch.create_index("ix_referral_attributions_participant_id", ["participant_id"])
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_referral_incentive_record_subject")
    with op.batch_alter_table("referral_eligibilities") as batch:
        batch.alter_column("participant_id", existing_type=sa.Integer(), nullable=False)
        batch.alter_column("promoter_id", existing_type=sa.Integer(), nullable=True)
        batch.create_foreign_key("fk_referral_eligibility_participant_tenant", "referral_participants", ["business_id", "participant_id"], ["business_id", "id"], ondelete="RESTRICT")
        batch.create_index("ix_referral_eligibilities_participant_id", ["participant_id"])

    op.create_table(
        "referral_participant_audits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("participant_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("actor_type", sa.String(30), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("previous_state", sa.Text(), nullable=True),
        sa.Column("new_state", sa.Text(), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["business_id", "participant_id"], ["referral_participants.business_id", "referral_participants.id"], ondelete="RESTRICT", name="fk_referral_participant_audit_tenant"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "request_id", name="uq_referral_participant_audit_request"),
    )
    op.create_index("ix_referral_participant_audits_business_id", "referral_participant_audits", ["business_id"])
    op.create_index("ix_referral_participant_audits_participant_id", "referral_participant_audits", ["participant_id"])
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE referral_participants ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE referral_participant_audits ENABLE ROW LEVEL SECURITY")
    _append_only(bind)
    _replace_record_guard(bind)


def downgrade():
    bind = op.get_bind()
    non_legacy = bind.execute(sa.text(
        "SELECT COUNT(*) FROM referral_participants WHERE participant_type != 'external_promoter'"
    )).scalar_one()
    audits = bind.execute(sa.text("SELECT COUNT(*) FROM referral_participant_audits")).scalar_one()
    if non_legacy or audits:
        raise RuntimeError("Downgrade bloqueado: existe participación o auditoría 8C.2B.")
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_referral_participant_audits_append_only ON referral_participant_audits")
        op.execute("DROP FUNCTION IF EXISTS prevent_referral_participant_audit_mutation()")
    else:
        op.execute("DROP TRIGGER IF EXISTS trg_referral_participant_audits_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_referral_participant_audits_no_delete")
        op.execute("DROP TRIGGER IF EXISTS trg_referral_incentive_record_subject")
    op.drop_table("referral_participant_audits")
    with op.batch_alter_table("referral_eligibilities") as batch:
        batch.drop_constraint("fk_referral_eligibility_participant_tenant", type_="foreignkey")
        batch.drop_index("ix_referral_eligibilities_participant_id")
        batch.drop_column("participant_id")
        batch.alter_column("promoter_id", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("referral_attributions") as batch:
        batch.drop_constraint("fk_referral_attribution_participant_tenant", type_="foreignkey")
        batch.drop_index("ix_referral_attributions_participant_id")
        batch.drop_column("participant_id")
        batch.alter_column("promoter_id", existing_type=sa.Integer(), nullable=False)
    op.drop_table("referral_participants")
    if bind.dialect.name == "sqlite":
        op.drop_index("uq_staff_business_id_id", table_name="staff")
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_referral_incentive_record_subject
            BEFORE INSERT ON referral_incentive_records
            WHEN NOT EXISTS (
                SELECT 1 FROM referral_eligibilities e
                WHERE e.business_id = NEW.business_id
                  AND e.id = NEW.eligibility_id
                  AND e.attribution_id = NEW.attribution_id
                  AND e.source_transaction_id = NEW.source_transaction_id
                  AND NEW.beneficiary_type = 'external_promoter'
                  AND e.promoter_id = NEW.beneficiary_id
            )
            BEGIN SELECT RAISE(ABORT, 'referral incentive subject mismatch'); END
        """)
    else:
        op.drop_constraint("uq_staff_business_id_id", "staff", type_="unique")
