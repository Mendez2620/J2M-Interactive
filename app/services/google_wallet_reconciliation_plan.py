"""Bounded, read-only preflight for recovering existing Google Wallet balances.

No provider I/O, writes, commands, or scheduled execution. A preview is evidence
for review only; a future executor must independently revalidate it under the
shared customer fence and persist its own outcome.
"""

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from types import SimpleNamespace
from datetime import datetime, timezone

from sqlalchemy import or_, select
from flask import current_app
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from app.extensions import db
from app.models import Business, Customer, GoogleWalletSyncJob, LoyaltyLedgerEntry, Transaction


MAX_CUSTOMERS = 100
MAX_EXECUTION_BATCH = 25
MAX_HISTORY = 5000
_QUANTUM = Decimal("0.000001")
_JOB_STATES = {"pending", "processing", "completed", "failed"}


@dataclass(frozen=True, repr=False)
class ReconciliationCandidate:
    """Internal snapshot; never serialize to public responses or logs."""
    business_id: int
    customer_id: int
    transaction_id: int
    ledger_id: int
    job_id: int | None
    points: Decimal
    stamps: int
    loyalty_type: str
    reward_limit: int
    balance_label: str | None


@dataclass(frozen=True, repr=False)
class ReconciliationPreview:
    business_active: bool
    scanned: int
    counts: dict
    candidates: tuple[ReconciliationCandidate, ...]
    next_after_customer_id: int | None


def _points(value):
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("non-finite points")
    return amount.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def _stamps(value):
    if type(value) is not int:
        raise ValueError("non-integral stamps")
    return value


def _balance(row, prefix):
    points = _points(row["points_" + prefix])
    stamps = _stamps(row["stamps_" + prefix])
    if points < 0 or stamps < 0:
        raise ValueError("negative balance")
    return points, stamps


def _rows(connection, table, columns, condition):
    return connection.execute(
        select(*(table.c[name] for name in columns)).where(condition)
        .order_by(table.c.id).limit(MAX_HISTORY + 1)
    ).mappings().all()


def _inspect_customer(connection, business, customer):
    business_id, customer_id = business["id"], customer["id"]
    tx = Transaction.__table__
    ledger = LoyaltyLedgerEntry.__table__
    job = GoogleWalletSyncJob.__table__
    transaction_ids = select(tx.c.id).where(tx.c.customer_id == customer_id)
    transactions = _rows(connection, tx, (
        "id", "business_id", "customer_id", "type", "points_amount", "stamps_amount",
    ), tx.c.customer_id == customer_id)
    entries = _rows(connection, ledger, (
        "id", "business_id", "customer_id", "baseline_customer_id", "transaction_id",
        "operation_type", "points_delta", "stamps_delta", "points_before", "points_after",
        "stamps_before", "stamps_after",
    ), or_(ledger.c.customer_id == customer_id,
           ledger.c.baseline_customer_id == customer_id,
           ledger.c.transaction_id.in_(transaction_ids)))
    jobs = _rows(connection, job, (
        "id", "business_id", "customer_id", "transaction_id", "status",
    ), or_(job.c.customer_id == customer_id, job.c.transaction_id.in_(transaction_ids)))
    if any(len(rows) > MAX_HISTORY for rows in (transactions, entries, jobs)):
        return "history_limit", None

    for row in (*transactions, *entries, *jobs):
        if row["business_id"] != business_id or row["customer_id"] != customer_id:
            return "invalid_linkage", None
    by_id = {row["id"]: row for row in transactions}
    if any(row["transaction_id"] not in by_id for row in jobs):
        return "invalid_linkage", None
    if any(row["status"] not in _JOB_STATES for row in jobs):
        return "unknown_job_status", None
    if not transactions:
        return "no_transactions", None
    if not entries or entries[0]["operation_type"] != "baseline":
        return "missing_baseline", None
    baseline, *movements = entries
    if (baseline["baseline_customer_id"] != customer_id
            or baseline["transaction_id"] is not None
            or any(row["baseline_customer_id"] is not None for row in movements)):
        return "invalid_ledger", None
    if [row["transaction_id"] for row in movements] != [row["id"] for row in transactions]:
        return "incomplete_ledger", None

    try:
        balance = _balance(baseline, "after")
        if (_balance(baseline, "before") != balance
                or _points(baseline["points_delta"]) != 0
                or _stamps(baseline["stamps_delta"]) != 0):
            return "invalid_ledger", None
        for entry in movements:
            transaction = by_id[entry["transaction_id"]]
            kind = transaction["type"]
            if kind not in {"earn", "redeem"} or entry["operation_type"] != kind:
                return "invalid_ledger", None
            points = _points(transaction["points_amount"])
            stamps = _stamps(transaction["stamps_amount"])
            if points < 0 or stamps < 0:
                return "invalid_ledger", None
            sign = 1 if kind == "earn" else -1
            delta = (points * sign, stamps * sign)
            if (_points(entry["points_delta"]), _stamps(entry["stamps_delta"])) != delta:
                return "invalid_ledger", None
            if _balance(entry, "before") != balance:
                return "invalid_ledger", None
            expected = (_points(balance[0] + delta[0]), balance[1] + delta[1])
            balance = _balance(entry, "after")
            if balance != expected:
                return "invalid_ledger", None
        if balance != (_points(customer["current_points"]), _stamps(customer["current_stamps"])):
            return "balance_mismatch", None
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return "invalid_ledger", None

    if any(row["status"] in {"pending", "processing"} for row in jobs):
        return "worker_pending", None
    latest = transactions[-1]
    matching = [row for row in jobs if row["transaction_id"] == latest["id"]]
    if len(matching) > 1:
        return "invalid_linkage", None
    latest_job = matching[0] if matching else None
    if latest_job and latest_job["status"] == "completed":
        return "already_covered", None

    if business["loyalty_type"] not in {"points", "stamps"}:
        return "invalid_program", None
    if business["loyalty_type"] == "stamps" and business["stamps_reward_limit"] <= 0:
        return "invalid_program", None
    return "eligible", ReconciliationCandidate(
        business_id, customer_id, latest["id"], movements[-1]["id"],
        latest_job["id"] if latest_job else None, balance[0], balance[1],
        business["loyalty_type"], business["stamps_reward_limit"],
        business["wallet_balance_label"],
    )


def preview_google_wallet_reconciliation(business_id, *, limit=50, after_customer_id=0, customer_ids=None):
    """Inspect a keyset page of customers on one consistent local DB snapshot.

    Uses a dedicated connection; callers must not share a SQLite in-memory
    connection with an open transaction. Production PostgreSQL uses a read-only
    repeatable-read transaction. A page is not authorization to update a pass.
    """
    if type(business_id) is not int or business_id < 1:
        raise ValueError("Se requiere negocio explícito.")
    if type(limit) is not int or not 1 <= limit <= MAX_CUSTOMERS:
        raise ValueError("Límite de clientes inválido.")
    if type(after_customer_id) is not int or after_customer_id < 0:
        raise ValueError("Cursor inválido.")
    if customer_ids is not None and (
        not 1 <= len(customer_ids) <= MAX_EXECUTION_BATCH
        or any(type(item) is not int or item < 1 for item in customer_ids)
        or len(set(customer_ids)) != len(customer_ids)
    ):
        raise ValueError("Selección inválida.")
    b, c = Business.__table__, Customer.__table__
    with db.engine.connect() as connection:
        dialect = connection.dialect.name
        if dialect == "postgresql":
            connection = connection.execution_options(
                isolation_level="REPEATABLE READ", postgresql_readonly=True,
            )
        elif dialect != "sqlite":
            raise RuntimeError("Base de datos no compatible con reconciliación.")
        with connection.begin():
            if dialect == "sqlite":
                # The legacy sqlite driver does not start a transaction on SELECT.
                connection.exec_driver_sql("BEGIN")
            business = connection.execute(select(
                b.c.id, b.c.is_active, b.c.loyalty_type, b.c.stamps_reward_limit,
                b.c.wallet_balance_label,
            ).where(b.c.id == business_id)).mappings().one_or_none()
            if business is None:
                raise ValueError("Negocio no encontrado.")
            if not business["is_active"]:
                return ReconciliationPreview(False, 0, {}, (), None)
            selection = select(
                c.c.id, c.c.current_points, c.c.current_stamps,
            ).where(c.c.business_id == business_id)
            selection = selection.where(
                c.c.id > after_customer_id if customer_ids is None else c.c.id.in_(customer_ids)
            )
            customers = connection.execute(selection.order_by(c.c.id).limit(limit + 1)).mappings().all()
            counts = Counter()
            candidates = []
            for customer in customers[:limit]:
                status, candidate = _inspect_customer(connection, business, customer)
                counts[status] += 1
                if candidate is not None:
                    candidates.append(candidate)
            next_id = customers[limit - 1]["id"] if len(customers) > limit else None
            return ReconciliationPreview(
                True, min(limit, len(customers)), dict(counts), tuple(candidates), next_id,
            )


def execute_google_wallet_reconciliation(candidate, *, business_id, service):
    from app.services.google_wallet import GoogleWalletService
    from app.services.google_wallet_write_reservation import WalletWriteBlocked

    if (isinstance(service, GoogleWalletService) and service.mode == "real"
            and isinstance(candidate, ReconciliationCandidate)
            and candidate.business_id == business_id):
        try:
            with service.write_scope(SimpleNamespace(id=candidate.customer_id, business_id=business_id)):
                return _execute_google_wallet_reconciliation(candidate, business_id=business_id, service=service)
        except (WalletWriteBlocked, SQLAlchemyError, ValueError):
            return {"status": "retry", "reason": "write_reservation_unavailable"}
    return _execute_google_wallet_reconciliation(candidate, business_id=business_id, service=service)


def _execute_google_wallet_reconciliation(candidate, *, business_id, service):
    """Internal, explicitly invoked recovery of one previously inspected candidate.

    No endpoint, CLI, automatic execution or authorization by a customer ID.
    The future operational entrypoint must enforce administrator authorization.
    Confirmed remote success is recorded in the existing transaction outbox;
    skipped or uncertain results never enqueue ordinary creation-capable work.
    """
    from app.services.google_wallet_sync_service import _customer_lock

    if (not isinstance(candidate, ReconciliationCandidate)
            or type(business_id) is not int or business_id < 1
            or candidate.business_id != business_id
            or type(candidate.customer_id) is not int or candidate.customer_id < 1):
        return {"status": "error", "reason": "invalid_candidate"}
    if (current_app.config.get("GOOGLE_WALLET_MODE") != "real"
            or getattr(service, "mode", None) != "real"):
        return {"status": "disabled", "reason": "real_mode_required"}

    # Shared fence with normal balance sync and design publication. Never hold
    # the customer row lock over provider I/O, which would block purchases.
    with _customer_lock(candidate.customer_id) as acquired:
        if not acquired:
            return {"status": "retry", "reason": "customer_busy"}
        try:
            fresh = preview_google_wallet_reconciliation(
                business_id, limit=1, after_customer_id=candidate.customer_id - 1,
            )
        except ValueError:
            return {"status": "skipped", "reason": "stale_candidate"}
        except SQLAlchemyError:
            return {"status": "retry", "reason": "local_preflight_unavailable"}
        if fresh.candidates != (candidate,):
            return {"status": "skipped", "reason": "stale_candidate"}

        # Send only immutable, verified balance/program values. Do not use
        # an ORM identity map that may contain stale or uncommitted changes.
        business = SimpleNamespace(
            id=business_id, is_active=True, loyalty_type=candidate.loyalty_type,
            stamps_reward_limit=candidate.reward_limit,
            wallet_balance_label=candidate.balance_label,
        )
        customer = SimpleNamespace(
            id=candidate.customer_id, business_id=business_id, business=business,
            current_points=float(candidate.points), current_stamps=candidate.stamps,
        )
        try:
            result = service.reconcile_existing_loyalty_balance(
                customer, business_id=business_id,
            )
        except Exception:
            return {"status": "retry", "reason": "provider_exception"}
        if not isinstance(result, dict):
            return {"status": "retry", "reason": "provider_unconfirmed"}
        if result.get("status") != "success" or not isinstance(result.get("reason"), str) or result.get("reason") not in {
            "already_current", "balance_updated",
        }:
            # Only return a controlled classification, not arbitrary provider
            # payloads, identifiers, credentials, or diagnostic strings.
            status = result.get("status")
            if not isinstance(status, str) or status not in {"skipped", "error", "retry", "disabled"}:
                status = "retry"
            reason = result.get("reason")
            safe_reasons = {
                "object_not_found", "object_not_active", "business_inactive",
                "tenant_mismatch", "remote_identity_mismatch", "balance_unconfirmed",
                "invalid_provider_response", "real_mode_required",
            }
            response = {"status": status, "reason": reason if isinstance(reason, str)
                        and reason in safe_reasons else "provider_unconfirmed"}
            code = result.get("status_code")
            if type(code) is int and 400 <= code <= 599:
                response["status_code"] = code
            return response

        # The remote call was confirmed, but transactions may have arrived.
        # A short local write transaction locks the business/customer and
        # revalidates the full ledger before recording ONLY the inspected
        # transaction. The normal writer updates Customer before inserting its
        # transaction/ledger/job. New movements therefore cannot be silently
        # marked completed by this operation.
        b, c, job = Business.__table__, Customer.__table__, GoogleWalletSyncJob.__table__
        try:
            with db.engine.begin() as connection:
                if connection.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                elif connection.dialect.name != "postgresql":
                    raise RuntimeError("Base de datos no compatible con reconciliación.")
                business_row = connection.execute(select(
                    b.c.id, b.c.is_active, b.c.loyalty_type, b.c.stamps_reward_limit,
                    b.c.wallet_balance_label,
                ).where(b.c.id == business_id).with_for_update()).mappings().one_or_none()
                customer_row = connection.execute(select(
                    c.c.id, c.c.current_points, c.c.current_stamps,
                ).where(c.c.id == candidate.customer_id, c.c.business_id == business_id)
                 .with_for_update()).mappings().one_or_none()
                if (business_row is None or not business_row["is_active"]
                        or customer_row is None):
                    return {"status": "retry", "reason": "changed_during_provider_call"}
                status, verified = _inspect_customer(connection, business_row, customer_row)
                if status != "eligible" or verified != candidate:
                    return {"status": "retry", "reason": "changed_during_provider_call"}
                now = datetime.now(timezone.utc).replace(tzinfo=None)
                if candidate.job_id is None:
                    connection.execute(job.insert().values(
                        business_id=business_id, customer_id=candidate.customer_id,
                        transaction_id=candidate.transaction_id, status="completed",
                        attempts=0, created_at=now, updated_at=now,
                    ))
                else:
                    updated = connection.execute(job.update().where(
                        job.c.id == candidate.job_id,
                        job.c.business_id == business_id,
                        job.c.customer_id == candidate.customer_id,
                        job.c.transaction_id == candidate.transaction_id,
                        job.c.status == "failed",
                    ).values(status="completed", locked_at=None, next_attempt_at=None,
                             last_status_code=None, updated_at=now))
                    if updated.rowcount != 1:
                        return {"status": "retry", "reason": "changed_during_provider_call"}
        except IntegrityError:
            # A conflicting job must never be overwritten. A subsequent fresh
            # attempt can verify the remote balance without repeating a PATCH.
            return {"status": "retry", "reason": "local_completion_conflict"}
        except SQLAlchemyError:
            # Remote success cannot be declared locally when persistence fails.
            return {"status": "retry", "reason": "local_completion_unconfirmed"}
        return {"status": "success", "reason": result["reason"]}


def execute_google_wallet_reconciliation_batch(candidates, *, business_id, service):
    """Internal bounded batch, not an externally authorized operation.

    Validate the entire selection before any remote I/O. Stop on errors or
    retries instead of amplifying a provider/database outage. Only a verified
    completed transaction is persisted by the individual executor.
    """
    if (type(business_id) is not int or business_id < 1
            or not isinstance(candidates, (tuple, list))
            or not 1 <= len(candidates) <= MAX_EXECUTION_BATCH):
        raise ValueError("Lote de reconciliación inválido.")
    if any(not isinstance(item, ReconciliationCandidate)
           or item.business_id != business_id
           or type(item.customer_id) is not int or item.customer_id < 1
           for item in candidates):
        raise ValueError("Candidatura de otro negocio o inválida.")
    if len({item.customer_id for item in candidates}) != len(candidates):
        raise ValueError("Clientes duplicados en lote.")

    if (current_app.config.get("GOOGLE_WALLET_MODE") != "real"
            or getattr(service, "mode", None) != "real"):
        return {"requested": len(candidates), "processed": 0,
                "remaining": len(candidates), "counts": {}, "stop_reason": "real_mode_required"}

    counts = Counter()
    processed = 0
    stop_reason = None
    for candidate in sorted(candidates, key=lambda item: item.customer_id):
        try:
            result = execute_google_wallet_reconciliation(
                candidate, business_id=business_id, service=service,
            )
        except SQLAlchemyError:
            result = {"status": "retry", "reason": "local_execution_unconfirmed"}
        processed += 1
        counts[result["status"]] += 1
        if result["status"] in {"error", "retry", "disabled"}:
            stop_reason = result["reason"]
            break
    return {
        "requested": len(candidates), "processed": processed,
        "remaining": len(candidates) - processed, "counts": dict(counts),
        "stop_reason": stop_reason,
    }
