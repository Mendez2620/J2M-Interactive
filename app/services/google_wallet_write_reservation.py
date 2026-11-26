"""Durable ownership of a Google Wallet resource across database sessions."""

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import text


@dataclass(frozen=True)
class Reservation:
    operation_id: str
    owner_token: str


def reserve(engine, *, kind, issuer, resource_id, business_id, customer_id=None):
    if kind not in {"loyaltyObject", "loyaltyClass"} or not resource_id.startswith(issuer + "."):
        raise ValueError("Invalid Wallet resource")
    if not issuer or (kind == "loyaltyObject") != (customer_id is not None):
        raise ValueError("Invalid Wallet ownership")
    operation = Reservation(uuid4().hex, uuid4().hex)
    with engine.begin() as connection:
        if customer_id is not None and connection.execute(text(
            "SELECT 1 FROM customers WHERE id=:customer AND business_id=:business"
        ), {"customer": customer_id, "business": business_id}).scalar() != 1:
            raise ValueError("Invalid Wallet ownership")
        inserted = connection.execute(text(
            "INSERT INTO google_wallet_write_reservations "
            "(operation_id, owner_token, issuer, kind, resource_id, business_id, "
            "customer_id, state, active_slot, updated_at) VALUES "
            "(:operation, :owner, :issuer, :kind, :resource, :business, :customer, "
            "'reserved', 1, :now) "
            "ON CONFLICT (issuer, kind, resource_id, active_slot) DO NOTHING"
        ), {"operation": operation.operation_id, "owner": operation.owner_token,
            "issuer": issuer, "kind": kind, "resource": resource_id,
            "business": business_id, "customer": customer_id, "now": _now()})
        return operation if inserted.rowcount == 1 else None


_TRANSITIONS = {
    ("reserved", "started"), ("reserved", "not_sent"),
    ("started", "uncertain"), ("started", "confirmed"), ("started", "rejected"),
    ("uncertain", "confirmed"), ("uncertain", "rejected"),
    ("confirmed", "completed"),
}
_TERMINAL = {"not_sent", "rejected", "completed"}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def transition(engine, reservation, *, previous, state):
    """The original owner can confirm a late response using a fresh connection."""
    if (previous, state) not in _TRANSITIONS:
        raise ValueError("Invalid Wallet operation transition")
    with engine.begin() as connection:
        changed = connection.execute(text(
            "UPDATE google_wallet_write_reservations SET state=:state, "
            "active_slot=:slot, updated_at=:now WHERE operation_id=:operation "
            "AND owner_token=:owner AND state=:previous AND active_slot=1"
        ), {"state": state, "slot": None if state in _TERMINAL else 1, "now": _now(),
            "operation": reservation.operation_id, "owner": reservation.owner_token,
            "previous": previous})
        return changed.rowcount == 1
