"""Allow inactive customer invitation identities before explicit consent.

Revision ID: 6a7b8c9d0e1f
Revises: 5f6a7b8c9d0e
"""

from alembic import op
import sqlalchemy as sa


revision = "6a7b8c9d0e1f"
down_revision = "5f6a7b8c9d0e"
branch_labels = None
depends_on = None


_OLD = (
    "participant_type != 'customer_ambassador' OR "
    "(consent_version IS NOT NULL AND consented_at IS NOT NULL "
    "AND consent_evidence_hash IS NOT NULL)"
)
_NEW = (
    "participant_type != 'customer_ambassador' OR NOT is_enabled OR "
    "(consent_version IS NOT NULL AND consented_at IS NOT NULL "
    "AND consent_evidence_hash IS NOT NULL)"
)


def _replace_constraint(expression):
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS trg_referral_incentive_record_subject")
    with op.batch_alter_table("referral_participants") as batch:
        batch.drop_constraint("ck_referral_participant_customer_consent", type_="check")
        batch.create_check_constraint(
            "ck_referral_participant_customer_consent", expression,
        )
    if bind.dialect.name == "sqlite":
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


def upgrade():
    _replace_constraint(_NEW)


def downgrade():
    bind = op.get_bind()
    incompatible = bind.execute(sa.text("""
        SELECT COUNT(*) FROM referral_participants
        WHERE participant_type = 'customer_ambassador'
          AND (consent_version IS NULL OR consented_at IS NULL
               OR consent_evidence_hash IS NULL)
    """)).scalar_one()
    if incompatible:
        raise RuntimeError(
            "Downgrade bloqueado: existen invitaciones de clientes sin consentimiento "
            "que la revisión anterior no puede representar sin pérdida."
        )
    _replace_constraint(_OLD)
