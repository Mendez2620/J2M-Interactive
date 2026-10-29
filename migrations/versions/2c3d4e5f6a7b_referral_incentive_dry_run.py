"""add disabled referral incentive rules and dry-run records

Revision ID: 2c3d4e5f6a7b
Revises: 1b2c3d4e5f6a
"""

from alembic import op
import sqlalchemy as sa


revision = "2c3d4e5f6a7b"
down_revision = "1b2c3d4e5f6a"
branch_labels = None
depends_on = None


def _append_only_guards(bind):
    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_referral_incentive_history_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'referral incentive history is append-only';
            END;
            $$ LANGUAGE plpgsql
        """)
        for table, trigger in (
            ("referral_incentive_rule_audits", "trg_referral_rule_audits_append_only"),
            ("referral_incentive_records", "trg_referral_incentive_records_append_only"),
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
            op.execute(f"""
                CREATE TRIGGER {trigger}
                BEFORE UPDATE OR DELETE ON {table}
                FOR EACH ROW EXECUTE FUNCTION prevent_referral_incentive_history_mutation()
            """)
    elif bind.dialect.name == "sqlite":
        for table, prefix in (
            ("referral_incentive_rule_audits", "referral_rule_audits"),
            ("referral_incentive_records", "referral_incentive_records"),
        ):
            op.execute(f"""
                CREATE TRIGGER IF NOT EXISTS trg_{prefix}_no_update
                BEFORE UPDATE ON {table}
                BEGIN SELECT RAISE(ABORT, 'referral incentive history is append-only'); END
            """)
            op.execute(f"""
                CREATE TRIGGER IF NOT EXISTS trg_{prefix}_no_delete
                BEFORE DELETE ON {table}
                BEGIN SELECT RAISE(ABORT, 'referral incentive history is append-only'); END
            """)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    incentive_tables = {
        "referral_incentive_rules", "referral_incentive_rule_audits",
        "referral_incentive_records",
    }
    if "referral_incentive_rules" in tables:
        if not incentive_tables <= tables:
            raise RuntimeError("Esquema de incentivos previo incompleto; no se marcará la revisión.")
        expected = {
            "referral_incentive_rules": {
                "id", "business_id", "beneficiary_type", "loyalty_program",
                "reward_type", "reward_amount", "eligible_condition", "period_limit",
                "period_unit", "valid_from", "valid_until", "status", "version",
                "created_by_type", "created_by_id", "updated_by_type", "updated_by_id",
                "created_at", "updated_at",
            },
            "referral_incentive_rule_audits": {
                "id", "business_id", "rule_id", "rule_version", "action",
                "actor_type", "actor_id", "previous_snapshot", "new_snapshot",
                "reason", "request_id", "request_fingerprint", "created_at",
            },
            "referral_incentive_records": {
                "id", "business_id", "eligibility_id", "rule_id", "rule_version",
                "beneficiary_type", "beneficiary_id", "event_key", "status", "reason",
                "reverses_record_id", "audit_reference", "created_by_type", "created_by_id",
                "created_at",
            },
        }
        if any(not columns <= {item["name"] for item in inspector.get_columns(table)}
               for table, columns in expected.items()):
            raise RuntimeError("Esquema de incentivos previo incompatible; no se marcará la revisión.")
        required_uniques = {
            "referral_incentive_rules": {"uq_referral_incentive_rule_business_id_id"},
            "referral_incentive_rule_audits": {"uq_referral_rule_audit_business_request"},
            "referral_incentive_records": {
                "uq_referral_incentive_record_event", "uq_referral_incentive_record_subject",
                "uq_referral_incentive_record_business_id_id",
            },
        }
        if any(not names <= {item["name"] for item in inspector.get_unique_constraints(table)}
               for table, names in required_uniques.items()):
            raise RuntimeError("Esquema de incentivos previo incompatible; faltan restricciones únicas.")
        if bind.dialect.name == "postgresql":
            for table in incentive_tables:
                op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        _append_only_guards(bind)
        return
    op.create_table(
        "referral_incentive_rules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("beneficiary_type", sa.String(30), nullable=False),
        sa.Column("loyalty_program", sa.String(20), nullable=False),
        sa.Column("reward_type", sa.String(30), nullable=False),
        sa.Column("reward_amount", sa.Numeric(18, 6), nullable=False),
        sa.Column("eligible_condition", sa.String(50), server_default="first_verified_purchase", nullable=False),
        sa.Column("period_limit", sa.Integer(), nullable=True),
        sa.Column("period_unit", sa.String(10), nullable=True),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(20), server_default="disabled", nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by_type", sa.String(30), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("updated_by_type", sa.String(30), nullable=False),
        sa.Column("updated_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("beneficiary_type IN ('external_promoter','staff','customer_ambassador')", name="ck_referral_incentive_rule_beneficiary"),
        sa.CheckConstraint("loyalty_program IN ('points','stamps')", name="ck_referral_incentive_rule_program"),
        sa.CheckConstraint("reward_type IN ('points','stamps')", name="ck_referral_incentive_rule_reward"),
        sa.CheckConstraint("reward_amount > 0", name="ck_referral_incentive_rule_amount"),
        sa.CheckConstraint("eligible_condition = 'first_verified_purchase'", name="ck_referral_incentive_rule_condition"),
        sa.CheckConstraint("status IN ('draft','disabled')", name="ck_referral_incentive_rule_status"),
        sa.CheckConstraint("(period_limit IS NULL AND period_unit IS NULL) OR (period_limit > 0 AND period_unit IN ('day','month'))", name="ck_referral_incentive_rule_period"),
        sa.CheckConstraint("valid_until IS NULL OR valid_from IS NULL OR valid_until > valid_from", name="ck_referral_incentive_rule_validity"),
        sa.ForeignKeyConstraint(["business_id"], ["businesses.id"], ondelete="RESTRICT", name="fk_referral_incentive_rule_business"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "id", name="uq_referral_incentive_rule_business_id_id"),
    )
    op.create_index("ix_referral_incentive_rules_business_id", "referral_incentive_rules", ["business_id"])
    op.create_index("ix_referral_incentive_rules_status", "referral_incentive_rules", ["status"])

    op.create_table(
        "referral_incentive_rule_audits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("actor_type", sa.String(30), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=False),
        sa.Column("previous_snapshot", sa.Text(), nullable=True),
        sa.Column("new_snapshot", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["business_id", "rule_id"],
            ["referral_incentive_rules.business_id", "referral_incentive_rules.id"],
            ondelete="RESTRICT", name="fk_referral_rule_audit_rule_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "request_id", name="uq_referral_rule_audit_business_request"),
    )
    op.create_index("ix_referral_incentive_rule_audits_business_id", "referral_incentive_rule_audits", ["business_id"])
    op.create_index("ix_referral_incentive_rule_audits_rule_id", "referral_incentive_rule_audits", ["rule_id"])

    op.create_table(
        "referral_incentive_records",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("eligibility_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("beneficiary_type", sa.String(30), nullable=False),
        sa.Column("beneficiary_id", sa.Integer(), nullable=False),
        sa.Column("event_key", sa.String(120), nullable=False),
        sa.Column("status", sa.String(20), server_default="proposed", nullable=False),
        sa.Column("reverses_record_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("audit_reference", sa.String(120), nullable=False),
        sa.Column("created_by_type", sa.String(30), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("beneficiary_type IN ('external_promoter','staff','customer_ambassador')", name="ck_referral_incentive_record_beneficiary"),
        sa.CheckConstraint("status IN ('proposed','voided')", name="ck_referral_incentive_record_status"),
        sa.CheckConstraint("(status = 'proposed' AND reverses_record_id IS NULL) OR (status = 'voided' AND reverses_record_id IS NOT NULL)", name="ck_referral_incentive_record_reversal"),
        sa.ForeignKeyConstraint(
            ["business_id", "eligibility_id"],
            ["referral_eligibilities.business_id", "referral_eligibilities.id"],
            ondelete="RESTRICT", name="fk_referral_incentive_record_eligibility_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "rule_id"],
            ["referral_incentive_rules.business_id", "referral_incentive_rules.id"],
            ondelete="RESTRICT", name="fk_referral_incentive_record_rule_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "reverses_record_id"],
            ["referral_incentive_records.business_id", "referral_incentive_records.id"],
            ondelete="RESTRICT", name="fk_referral_incentive_record_reversal_tenant",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "id", name="uq_referral_incentive_record_business_id_id"),
        sa.UniqueConstraint("business_id", "event_key", name="uq_referral_incentive_record_event"),
        sa.UniqueConstraint(
            "business_id", "eligibility_id", "rule_id", "rule_version",
            "beneficiary_type", "beneficiary_id", "status",
            name="uq_referral_incentive_record_subject",
        ),
    )
    op.create_index("ix_referral_incentive_records_business_id", "referral_incentive_records", ["business_id"])
    op.create_index("ix_referral_incentive_records_eligibility_id", "referral_incentive_records", ["eligibility_id"])
    op.create_index("ix_referral_incentive_records_rule_id", "referral_incentive_records", ["rule_id"])

    if bind.dialect.name == "postgresql":
        for table in (
            "referral_incentive_rules", "referral_incentive_rule_audits",
            "referral_incentive_records",
        ):
            op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    _append_only_guards(bind)


def downgrade():
    bind = op.get_bind()
    history = bind.execute(sa.text(
        "SELECT (SELECT COUNT(*) FROM referral_incentive_rule_audits) + "
        "(SELECT COUNT(*) FROM referral_incentive_records)"
    )).scalar_one()
    if history:
        raise RuntimeError(
            "Downgrade bloqueado: existe historial de reglas o incentivos que debe conservarse."
        )
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_referral_rule_audits_append_only ON referral_incentive_rule_audits")
        op.execute("DROP TRIGGER IF EXISTS trg_referral_incentive_records_append_only ON referral_incentive_records")
        op.execute("DROP FUNCTION IF EXISTS prevent_referral_incentive_history_mutation()")
    elif bind.dialect.name == "sqlite":
        for trigger in (
            "trg_referral_rule_audits_no_update", "trg_referral_rule_audits_no_delete",
            "trg_referral_incentive_records_no_update", "trg_referral_incentive_records_no_delete",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.drop_table("referral_incentive_records")
    op.drop_table("referral_incentive_rule_audits")
    op.drop_table("referral_incentive_rules")
