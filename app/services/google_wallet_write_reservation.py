"""Durable ownership of a Google Wallet resource across database sessions."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from uuid import uuid4

from sqlalchemy import text


@dataclass(frozen=True)
class Reservation:
    operation_id: str
    owner_token: str = field(repr=False)


def reserve(engine, *, kind, issuer, resource_id, business_id, customer_id=None):
    if (not isinstance(issuer, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", issuer)
            or type(business_id) is not int or not 0 < business_id <= 2147483647
            or kind not in ("loyaltyObject", "loyaltyClass")):
        raise ValueError("Invalid Wallet ownership")
    expected = f"{issuer}.business_{business_id}"
    if kind == "loyaltyObject":
        if type(customer_id) is not int or not 0 < customer_id <= 2147483647:
            raise ValueError("Invalid Wallet ownership")
        expected += f"_customer_{customer_id}"
    elif customer_id is not None:
        raise ValueError("Invalid Wallet ownership")
    if not isinstance(resource_id, str) or resource_id != expected:
        raise ValueError("Invalid Wallet resource")
    operation = Reservation(uuid4().hex, uuid4().hex)
    with engine.begin() as connection:
        if connection.execute(text("SELECT 1 FROM businesses WHERE id=:business"),
                              {"business": business_id}).scalar() != 1:
            raise ValueError("Invalid Wallet ownership")
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


def transition(engine, reservation, *, previous, state, remote_status=None):
    """The original owner can confirm a late response using a fresh connection."""
    if (previous, state) not in _TRANSITIONS:
        raise ValueError("Invalid Wallet operation transition")
    if remote_status is not None and (
        type(remote_status) is not int or not 100 <= remote_status <= 599
        or state not in {"confirmed", "rejected", "uncertain"}
    ):
        raise ValueError("Invalid Wallet response evidence")
    with engine.begin() as connection:
        changed = connection.execute(text(
            "UPDATE google_wallet_write_reservations SET state=:state, "
            "active_slot=:slot, updated_at=:now, "
            "started_at=CASE WHEN :state='started' THEN :now ELSE started_at END, "
            "remote_resolved_at=CASE WHEN :state IN ('confirmed','rejected') "
            "THEN :now ELSE remote_resolved_at END, "
            "remote_status=COALESCE(:remote_status, remote_status) "
            "WHERE operation_id=:operation "
            "AND owner_token=:owner AND state=:previous AND active_slot=1"
        ), {"state": state, "slot": None if state in _TERMINAL else 1, "now": _now(),
            "operation": reservation.operation_id, "owner": reservation.owner_token,
            "previous": previous, "remote_status": remote_status})
        return changed.rowcount == 1
