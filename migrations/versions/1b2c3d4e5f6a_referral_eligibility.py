"""add persistent referral eligibility review

Revision ID: 1b2c3d4e5f6a
Revises: 0a1b2c3d4e5f
"""

from alembic import op
import sqlalchemy as sa

revision = "1b2c3d4e5f6a"
down_revision = "0a1b2c3d4e5f"
branch_labels = None
depends_on = None


def _create_append_only_guards(bind):
    if bind.dialect.name == "postgresql":
        op.execute("""
            CREATE OR REPLACE FUNCTION prevent_referral_decision_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'referral eligibility decisions are append-only';
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute("""
            DROP TRIGGER IF EXISTS trg_referral_decisions_append_only
            ON referral_eligibility_decisions
        """)
        op.execute("""
            CREATE TRIGGER trg_referral_decisions_append_only
            BEFORE UPDATE OR DELETE ON referral_eligibility_decisions
            FOR EACH ROW EXECUTE FUNCTION prevent_referral_decision_mutation()
        """)
    elif bind.dialect.name == "sqlite":
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_referral_decisions_no_update
            BEFORE UPDATE ON referral_eligibility_decisions
            BEGIN SELECT RAISE(ABORT, 'referral eligibility decisions are append-only'); END
        """)
        op.execute("""
            CREATE TRIGGER IF NOT EXISTS trg_referral_decisions_no_delete
            BEFORE DELETE ON referral_eligibility_decisions
            BEGIN SELECT RAISE(ABORT, 'referral eligibility decisions are append-only'); END
        """)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "referral_eligibilities" in inspector.get_table_names():
        tables = set(inspector.get_table_names())
        if "referral_eligibility_decisions" not in tables:
            raise RuntimeError("Esquema de elegibilidad previo incompleto: falta el historial.")
        required_columns = {
            "id", "business_id", "attribution_id", "promoter_id", "customer_id",
            "status", "source_transaction_id", "evidence_reference_hash", "evidence_type",
            "evidence_key_id",
            "reviewed_by_staff_id", "reviewed_at", "created_at", "updated_at", "version",
        }
        decision_columns = {
            "id", "business_id", "eligibility_id", "actor_staff_id", "from_status",
            "to_status", "source_transaction_id", "evidence_reference_hash", "evidence_type",
            "evidence_key_id", "reason",
            "request_id", "request_fingerprint", "created_at",
        }
        actual_columns = {item["name"] for item in inspector.get_columns("referral_eligibilities")}
        actual_decision_columns = {
            item["name"] for item in inspector.get_columns("referral_eligibility_decisions")
        }
        unique_names = {
            item["name"] for item in inspector.get_unique_constraints("referral_eligibilities")
        }
        decision_unique_names = {
            item["name"] for item in inspector.get_unique_constraints("referral_eligibility_decisions")
        }
        eligibility_fks = inspector.get_foreign_keys("referral_eligibilities")
        fk_names = {item["name"] for item in eligibility_fks}
        decision_fks = inspector.get_foreign_keys("referral_eligibility_decisions")
        attribution_fk = next(
            (item for item in eligibility_fks
             if item["name"] == "fk_referral_eligibility_attribution_tenant"), None
        )
        decision_fk = next(
            (item for item in decision_fks
             if item["name"] == "fk_referral_decision_eligibility_tenant"), None
        )
        if (
            not required_columns <= actual_columns
            or not decision_columns <= actual_decision_columns
            or not {"uq_referral_eligibility_business_id_id", "uq_referral_evidence_business_hash"} <= unique_names
            or "uq_referral_decision_business_request" not in decision_unique_names
            or not {
                "fk_referral_eligibility_attribution_tenant",
                "fk_referral_eligibility_promoter_tenant",
                "fk_referral_eligibility_customer_tenant",
                "fk_referral_eligibility_transaction_tenant",
            } <= fk_names
            or not attribution_fk
            or str(attribution_fk.get("options", {}).get("ondelete", "")).upper() != "RESTRICT"
            or not decision_fk
            or str(decision_fk.get("options", {}).get("ondelete", "")).upper() != "RESTRICT"
        ):
            raise RuntimeError("Esquema de elegibilidad previo incompatible; no se marcará la revisión.")
        if bind.dialect.name == "postgresql":
            op.execute("ALTER TABLE referral_eligibilities ENABLE ROW LEVEL SECURITY")
            op.execute("ALTER TABLE referral_eligibility_decisions ENABLE ROW LEVEL SECURITY")
        _create_append_only_guards(bind)
        return
    with op.batch_alter_table("referral_attributions") as batch:
        batch.create_unique_constraint(
            "uq_referral_attribution_business_id_id", ["business_id", "id"]
        )
    with op.batch_alter_table("transactions") as batch:
        batch.create_unique_constraint("uq_transactions_business_id_id", ["business_id", "id"])

    op.create_table(
        "referral_eligibilities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("attribution_id", sa.Integer(), nullable=False),
        sa.Column("promoter_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("source_transaction_id", sa.Integer(), nullable=True),
        sa.Column("evidence_reference_hash", sa.String(64), nullable=True),
        sa.Column("evidence_type", sa.String(30), nullable=True),
        sa.Column("evidence_key_id", sa.String(16), nullable=True),
        sa.Column("reviewed_by_staff_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','in_review','verified','rejected','revoked')",
            name="ck_referral_eligibility_status",
        ),
        sa.ForeignKeyConstraint(["reviewed_by_staff_id"], ["staff.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["business_id", "attribution_id"],
            ["referral_attributions.business_id", "referral_attributions.id"],
            name="fk_referral_eligibility_attribution_tenant", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "promoter_id"], ["promoters.business_id", "promoters.id"],
            name="fk_referral_eligibility_promoter_tenant", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            name="fk_referral_eligibility_customer_tenant", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["business_id", "source_transaction_id"], ["transactions.business_id", "transactions.id"],
            name="fk_referral_eligibility_transaction_tenant", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("attribution_id"),
        sa.UniqueConstraint("customer_id"),
        sa.UniqueConstraint("source_transaction_id"),
        sa.UniqueConstraint("business_id", "id", name="uq_referral_eligibility_business_id_id"),
        sa.UniqueConstraint("business_id", "evidence_reference_hash", name="uq_referral_evidence_business_hash"),
    )
    op.create_index("ix_referral_eligibilities_business_id", "referral_eligibilities", ["business_id"])
    op.create_index("ix_referral_eligibilities_promoter_id", "referral_eligibilities", ["promoter_id"])
    op.create_index("ix_referral_eligibilities_status", "referral_eligibilities", ["status"])

    attributions = sa.table(
        "referral_attributions",
        sa.column("id", sa.Integer), sa.column("business_id", sa.Integer),
        sa.column("promoter_id", sa.Integer), sa.column("customer_id", sa.Integer),
    )
    eligibility = sa.table(
        "referral_eligibilities",
        sa.column("business_id", sa.Integer), sa.column("attribution_id", sa.Integer),
        sa.column("promoter_id", sa.Integer), sa.column("customer_id", sa.Integer),
        sa.column("status", sa.String), sa.column("created_at", sa.DateTime),
        sa.column("updated_at", sa.DateTime), sa.column("version", sa.Integer),
    )
    bind.execute(eligibility.insert().from_select(
        ["business_id", "attribution_id", "promoter_id", "customer_id", "status",
         "created_at", "updated_at", "version"],
        sa.select(
            attributions.c.business_id, attributions.c.id, attributions.c.promoter_id,
            attributions.c.customer_id, sa.literal("pending"), sa.func.now(),
            sa.func.now(), sa.literal(0),
        ),
    ))

    op.create_table(
        "referral_eligibility_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("business_id", sa.Integer(), nullable=False),
        sa.Column("eligibility_id", sa.Integer(), nullable=False),
        sa.Column("actor_staff_id", sa.Integer(), nullable=True),
        sa.Column("from_status", sa.String(20), nullable=False),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("source_transaction_id", sa.Integer(), nullable=True),
        sa.Column("evidence_reference_hash", sa.String(64), nullable=True),
        sa.Column("evidence_type", sa.String(30), nullable=True),
        sa.Column("evidence_key_id", sa.String(16), nullable=True),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["actor_staff_id"], ["staff.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["business_id", "eligibility_id"],
            ["referral_eligibilities.business_id", "referral_eligibilities.id"],
            name="fk_referral_decision_eligibility_tenant", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "request_id", name="uq_referral_decision_business_request"),
    )
    op.create_index("ix_referral_eligibility_decisions_business_id", "referral_eligibility_decisions", ["business_id"])
    op.create_index("ix_referral_eligibility_decisions_eligibility_id", "referral_eligibility_decisions", ["eligibility_id"])

    if bind.dialect.name == "postgresql":
        # Internal tables: deny Data API-style public access even when created in public schema.
        op.execute("ALTER TABLE referral_eligibilities ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE referral_eligibility_decisions ENABLE ROW LEVEL SECURITY")
    _create_append_only_guards(bind)


def downgrade():
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT COUNT(*) FROM referral_eligibility_decisions")).scalar_one()
    if count:
        raise RuntimeError(
            "Downgrade bloqueado: existen decisiones de elegibilidad que deben conservarse."
        )
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_referral_decisions_append_only ON referral_eligibility_decisions")
        op.execute("DROP FUNCTION IF EXISTS prevent_referral_decision_mutation()")
    elif bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_referral_decisions_no_update")
        op.execute("DROP TRIGGER IF EXISTS trg_referral_decisions_no_delete")
    op.drop_table("referral_eligibility_decisions")
    op.drop_table("referral_eligibilities")
    with op.batch_alter_table("transactions") as batch:
        batch.drop_constraint("uq_transactions_business_id_id", type_="unique")
    with op.batch_alter_table("referral_attributions") as batch:
        batch.drop_constraint("uq_referral_attribution_business_id_id", type_="unique")
