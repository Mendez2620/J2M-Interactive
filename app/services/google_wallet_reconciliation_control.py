"""Internal reconciliation control; no routes, schedules or provider activation.

The actor ID must come from a trusted authenticated platform-admin context.
An integer ID and confirmation alone are not authentication. This service
intentionally has no external entrypoint. Audit writes use the existing
administrative history and independent transactions.
"""

import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from uuid import UUID

from flask import current_app
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import AccessAudit, Business, PlatformAdmin
from app.services.google_wallet_reconciliation_plan import (
    MAX_EXECUTION_BATCH, ReconciliationCandidate, execute_google_wallet_reconciliation,
)
from app.services.google_wallet_sync_service import _resource_lock


_NAMESPACE = 1246907726
_START = "wallet_reconciliation_started"
_ITEM_START = "wallet_reconciliation_item_started"
_ITEM_FINISH = "wallet_reconciliation_item_finished"
_FINISH = "wallet_reconciliation_finished"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _append(connection, action, *, admin_id, business_id, key, payload):
    connection.execute(AccessAudit.__table__.insert().values(
        action=action, platform_admin_id=admin_id, business_id=business_id,
        previous_state=key, new_state=_json(payload),
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
    ))


def _active_admin(connection, admin_id):
    table = PlatformAdmin.__table__
    return connection.execute(select(table.c.id).where(
        table.c.id == admin_id, table.c.is_active.is_(True),
    )).scalar_one_or_none() is not None


def _existing(connection, business_id, key, admin_id, fingerprint):
    table = AccessAudit.__table__
    rows = connection.execute(select(
        table.c.action, table.c.platform_admin_id, table.c.new_state,
    ).where(table.c.business_id == business_id, table.c.previous_state == key,
            table.c.action.in_((_START, _FINISH))).order_by(table.c.id)).mappings().all()
    if not rows:
        return None
    if len(rows) not in (1, 2) or rows[0]["action"] != _START:
        return {"status": "unconfirmed", "reason": "audit_inconsistent"}
    try:
        start = json.loads(rows[0]["new_state"])
        if (rows[0]["platform_admin_id"] != admin_id
                or start.get("fingerprint") != fingerprint):
            return {"status": "error", "reason": "request_conflict"}
        if len(rows) == 1:
            # A previous process might have reached Google before losing its
            # local connection. Do not silently retry this request.
            return {"status": "unconfirmed", "reason": "request_incomplete"}
        if rows[1]["action"] != _FINISH or rows[1]["platform_admin_id"] != admin_id:
            return {"status": "unconfirmed", "reason": "audit_inconsistent"}
        report = json.loads(rows[1]["new_state"])
        if not isinstance(report, dict) or report.get("status") != "finished":
            return {"status": "unconfirmed", "reason": "audit_inconsistent"}
        return {**report, "replayed": True}
    except (ValueError, TypeError, AttributeError):
        return {"status": "unconfirmed", "reason": "audit_inconsistent"}


def run_reviewed_reconciliation(candidates, *, business_id, admin_id, request_id,
                                confirm, service):
    """Record a bounded, explicitly confirmed selection and each attempt.

    This entrypoint is internal only. The feature flag defaults to false and
    is not configured by environment or exposed to users in this phase.
    """
    if (type(admin_id) is not int or admin_id < 1
            or type(business_id) is not int or business_id < 1
            or confirm is not True):
        raise ValueError("Se requiere administrador, negocio y confirmación explícita.")
    try:
        valid_id = isinstance(request_id, str) and str(UUID(request_id)) == request_id
    except (ValueError, AttributeError):
        valid_id = False
    if not valid_id:
        raise ValueError("Identificador de solicitud inválido.")
    if (not isinstance(candidates, (list, tuple))
            or not 1 <= len(candidates) <= MAX_EXECUTION_BATCH
            or any(not isinstance(item, ReconciliationCandidate)
                   or item.business_id != business_id
                   or type(item.customer_id) is not int or item.customer_id < 1
                   for item in candidates)
            or len({item.customer_id for item in candidates}) != len(candidates)):
        raise ValueError("Selección de reconciliación inválida.")

    if (current_app.config.get("GOOGLE_WALLET_RECONCILIATION_ENABLED") is not True
            or current_app.config.get("GOOGLE_WALLET_MODE") != "real"
            or getattr(service, "mode", None) != "real"):
        return {"status": "disabled", "reason": "reconciliation_disabled"}

    ordered = sorted(candidates, key=lambda item: item.customer_id)
    # Store only a fingerprint and local reference IDs, not private balances,
    # names, credentials or arbitrary provider responses.
    fingerprint = hashlib.sha256(_json([asdict(item) for item in ordered]).encode()).hexdigest()
    key = _json({"request_id": request_id})
    audit_args = {"admin_id": admin_id, "business_id": business_id, "key": key}
    try:
        with _resource_lock(_NAMESPACE, business_id) as acquired:
            if not acquired:
                return {"status": "retry", "reason": "business_busy"}
            with db.engine.begin() as connection:
                if not _active_admin(connection, admin_id):
                    return {"status": "error", "reason": "admin_inactive"}
                previous = _existing(connection, business_id, key, admin_id, fingerprint)
                if previous is not None:
                    return previous
                b = Business.__table__
                active = connection.execute(select(b.c.is_active).where(
                    b.c.id == business_id,
                )).scalar_one_or_none()
                if active is not True:
                    return {"status": "error", "reason": "business_inactive"}
                _append(connection, _START, **audit_args, payload={
                    "fingerprint": fingerprint, "requested": len(ordered),
                })

            counts = Counter()
            processed = 0
            stop_reason = None
            for candidate in ordered:
                references = {"customer_id": candidate.customer_id,
                              "transaction_id": candidate.transaction_id,
                              "ledger_id": candidate.ledger_id}
                with db.engine.begin() as connection:
                    if not _active_admin(connection, admin_id):
                        stop_reason = "admin_inactive"
                        break
                    _append(connection, _ITEM_START, **audit_args, payload=references)

                # The committed start must precede any possible provider call.
                # An unexpected failure deliberately leaves the request open
                # for manual investigation rather than silently replaying it.
                result = execute_google_wallet_reconciliation(
                    candidate, business_id=business_id, service=service,
                )
                with db.engine.begin() as connection:
                    _append(connection, _ITEM_FINISH, **audit_args, payload={
                        **references, **result,
                    })
                processed += 1
                counts[result["status"]] += 1
                if result["status"] in {"error", "retry", "disabled"}:
                    stop_reason = result["reason"]
                    break

            report = {"status": "finished", "requested": len(ordered),
                      "processed": processed, "remaining": len(ordered) - processed,
                      "counts": dict(counts), "stop_reason": stop_reason}
            with db.engine.begin() as connection:
                _append(connection, _FINISH, **audit_args, payload=report)
            return {**report, "replayed": False}
    except SQLAlchemyError:
        return {"status": "unconfirmed", "reason": "audit_or_execution_unconfirmed"}
