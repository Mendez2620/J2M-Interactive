from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import and_, case, exists, or_, update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Customer, LoyaltyLedgerEntry, Transaction


POINT_QUANTUM = Decimal("0.000001")


def normalized_points(value):
    return float(Decimal(str(value)).quantize(POINT_QUANTUM, rounding=ROUND_HALF_UP))


class LoyaltyTransactionError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass
class LoyaltyTransactionResult:
    transaction: Transaction
    customer: Customer
    replay: bool = False


def _existing_result(business_id, request_id, fingerprint):
    existing = Transaction.query.filter_by(business_id=business_id, request_id=request_id).first()
    if not existing:
        return None
    if existing.request_fingerprint == fingerprint:
        return LoyaltyTransactionResult(existing, existing.customer, replay=True)
    raise LoyaltyTransactionError("Idempotency-Key ya fue utilizado para otra operación.", 409)


def process_loyalty_transaction(*, business, customer_id, staff_id, action, tx_type,
                                points_amount, stamps_amount, purchase_amount,
                                request_id, fingerprint):
    existing = _existing_result(business.id, request_id, fingerprint)
    if existing:
        return existing

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    points_delta = normalized_points(points_amount if tx_type == "earn" else -points_amount)
    stamps_delta = stamps_amount if tx_type == "earn" else -stamps_amount
    table = Customer.__table__
    conditions = [table.c.id == customer_id, table.c.business_id == business.id]
    values = {}

    if action.startswith("earn"):
        cutoff = now - timedelta(minutes=business.cooldown_minutes)
        recent_earn = exists().where(and_(
            Transaction.business_id == business.id,
            Transaction.customer_id == customer_id,
            Transaction.type == "earn",
            Transaction.timestamp > cutoff,
        ))
        conditions.append(or_(
            table.c.last_earn_at <= cutoff,
            and_(table.c.last_earn_at.is_(None), ~recent_earn),
        ))
        values["last_earn_at"] = case(
            (table.c.last_earn_at > now, table.c.last_earn_at), else_=now,
        )

    if business.loyalty_type == "points":
        if points_delta < 0:
            conditions.append(table.c.current_points >= abs(points_delta))
        values["current_points"] = table.c.current_points + points_delta
    else:
        if stamps_delta < 0:
            conditions.append(table.c.current_stamps >= abs(stamps_delta))
        values["current_stamps"] = table.c.current_stamps + stamps_delta

    try:
        row = db.session.execute(
            update(table).where(*conditions).values(**values).returning(
                table.c.current_points, table.c.current_stamps,
            )
        ).first()
        if row is None:
            db.session.rollback()
            customer = db.session.get(Customer, customer_id)
            if action.startswith("earn"):
                raise LoyaltyTransactionError("Operación bloqueada por cooldown.", 429)
            unit = "Puntos" if business.loyalty_type == "points" else "Sellos"
            raise LoyaltyTransactionError(f"{unit} insuficientes.", 400)

        points_after = normalized_points(row.current_points)
        stamps_after = row.current_stamps
        points_before = normalized_points(points_after - points_delta)
        stamps_before = stamps_after - stamps_delta

        transaction = Transaction(
            business_id=business.id, customer_id=customer_id, staff_id=staff_id,
            type=tx_type, points_amount=points_amount, stamps_amount=stamps_amount,
            purchase_amount=purchase_amount, request_id=request_id,
            request_fingerprint=fingerprint,
        )
        db.session.add(transaction)
        db.session.flush()

        baseline = LoyaltyLedgerEntry.query.filter_by(baseline_customer_id=customer_id).first()
        if not baseline:
            db.session.add(LoyaltyLedgerEntry(
                business_id=business.id, customer_id=customer_id,
                baseline_customer_id=customer_id, operation_type="baseline",
                points_before=points_before, points_after=points_before,
                stamps_before=stamps_before, stamps_after=stamps_before,
                points_delta=0.0, stamps_delta=0, created_at=now,
            ))
        db.session.add(LoyaltyLedgerEntry(
            business_id=business.id, customer_id=customer_id,
            transaction_id=transaction.id, staff_id=staff_id,
            operation_type=tx_type, points_delta=points_delta, stamps_delta=stamps_delta,
            points_before=points_before, points_after=points_after,
            stamps_before=stamps_before, stamps_after=stamps_after, created_at=now,
        ))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        winner = _existing_result(business.id, request_id, fingerprint)
        if winner:
            return winner
        raise LoyaltyTransactionError("Conflicto al registrar la operación.", 409)
    except LoyaltyTransactionError:
        raise
    except Exception:
        db.session.rollback()
        raise

    customer = db.session.get(Customer, customer_id)
    return LoyaltyTransactionResult(transaction, customer)


def reconcile_customer_balance(customer):
    baseline = LoyaltyLedgerEntry.query.filter_by(baseline_customer_id=customer.id).first()
    if not baseline:
        return {"verifiable": False, "consistent": None, "reason": "no_baseline"}
    entries = LoyaltyLedgerEntry.query.filter(
        LoyaltyLedgerEntry.customer_id == customer.id,
        LoyaltyLedgerEntry.transaction_id.isnot(None),
    ).order_by(LoyaltyLedgerEntry.id).all()
    points, stamps = baseline.points_after, baseline.stamps_after
    chain_valid = True
    for entry in entries:
        chain_valid = chain_valid and normalized_points(entry.points_before) == normalized_points(points)
        chain_valid = chain_valid and entry.stamps_before == stamps
        points = normalized_points(points + entry.points_delta)
        stamps += entry.stamps_delta
        chain_valid = chain_valid and normalized_points(entry.points_after) == points
        chain_valid = chain_valid and entry.stamps_after == stamps
    consistent = chain_valid and normalized_points(customer.current_points) == normalized_points(points) and customer.current_stamps == stamps
    return {
        "verifiable": True, "consistent": consistent,
        "expected_points": points, "actual_points": normalized_points(customer.current_points),
        "expected_stamps": stamps, "actual_stamps": customer.current_stamps,
        "entries": len(entries),
    }
