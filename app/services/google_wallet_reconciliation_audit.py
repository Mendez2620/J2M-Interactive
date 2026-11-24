"""Read-only inventory of local Google Wallet outbox coverage, scoped to one tenant."""

import json

import click
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import Business, Customer, GoogleWalletSyncJob, LoyaltyLedgerEntry, Transaction


_STATES = (
    "missing_job", "pending", "processing", "failed", "completed",
    "missing_ledger", "invalid_linkage", "unknown_job_status",
)


def google_wallet_reconciliation_audit(business_id):
    """Return aggregate local evidence, not a claim of remote pass issuance or balance.

    Only the latest recorded transaction per customer is classified. An older
    completed job cannot be evidence that a later transaction was queued.
    No provider client, queue mutation, ledger replay, or implicit flush.
    """
    if type(business_id) is not int or business_id < 1:
        raise ValueError("Se requiere un identificador de negocio positivo.")

    with db.session.no_autoflush:
        business = db.session.execute(
            select(Business.is_active).where(Business.id == business_id)
        ).one_or_none()
        if business is None:
            raise ValueError("Negocio no encontrado.")

        latest = select(
            Transaction.customer_id,
            func.max(Transaction.id).label("transaction_id"),
        ).where(Transaction.business_id == business_id).group_by(
            Transaction.customer_id
        ).subquery()
        ledger = LoyaltyLedgerEntry
        job = GoogleWalletSyncJob

        state = case(
            (or_(
                Customer.id.is_(None),
                Customer.business_id != Transaction.business_id,
                ledger.business_id != Transaction.business_id,
                ledger.customer_id != Transaction.customer_id,
                ledger.operation_type != Transaction.type,
                job.business_id != Transaction.business_id,
                job.customer_id != Transaction.customer_id,
            ), "invalid_linkage"),
            (ledger.id.is_(None), "missing_ledger"),
            (job.id.is_(None), "missing_job"),
            (job.status.in_(("pending", "processing", "failed", "completed")), job.status),
            else_="unknown_job_status",
        ).label("state")

        classified = select(state).select_from(Transaction).join(
            latest, latest.c.transaction_id == Transaction.id
        ).outerjoin(
            Customer, Customer.id == Transaction.customer_id
        ).outerjoin(
            ledger, ledger.transaction_id == Transaction.id
        ).outerjoin(
            job, job.transaction_id == Transaction.id
        ).where(Transaction.business_id == business_id).subquery()

        rows = db.session.execute(
            select(classified.c.state, func.count()).group_by(classified.c.state)
        ).all()

    counts = dict.fromkeys(_STATES, 0)
    for status, count in rows:
        counts[status] = int(count)
    return {
        "scope": "latest_local_transaction_per_customer",
        "read_only": True,
        "business_active": bool(business[0]),
        "customers_with_transactions": sum(counts.values()),
        "counts": counts,
        "remote_issuance_verified": False,
        "remote_balance_verified": False,
    }


def register_reconciliation_audit(app):
    @app.cli.command("audit-google-wallet-reconciliation")
    @click.option("--business-id", required=True, type=click.IntRange(min=1))
    def audit_google_wallet_reconciliation_command(business_id):
        """Inspect local outbox coverage for one business; never apply changes."""
        try:
            report = google_wallet_reconciliation_audit(business_id)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        except SQLAlchemyError:
            raise click.ClickException("No fue posible consultar el diagnóstico local.") from None
        click.echo(json.dumps(report, sort_keys=True))
