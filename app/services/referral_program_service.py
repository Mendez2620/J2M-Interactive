"""Tenant-scoped referral program switch with audited idempotency."""

import hashlib
import json

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Business, ReferralProgramAudit


class ReferralProgramError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _clean(value, label, maximum):
    value = value.strip() if isinstance(value, str) else ""
    if not value or len(value) > maximum:
        raise ReferralProgramError(f"{label} es obligatorio y admite hasta {maximum} caracteres.")
    return value


def configure_referral_program(*, business, enabled, actor_type, actor_id,
                               reason, request_id):
    if not isinstance(enabled, bool):
        raise ReferralProgramError("enabled debe ser booleano.")
    reason = _clean(reason, "El motivo", 500)
    request_id = _clean(request_id, "Idempotency-Key", 64)
    payload = {
        "business_id": business.id, "enabled": enabled,
        "actor_type": actor_type, "actor_id": actor_id, "reason": reason,
    }
    fingerprint = hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    existing = ReferralProgramAudit.query.filter_by(
        business_id=business.id, request_id=request_id,
    ).first()
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise ReferralProgramError("La clave idempotente ya se utilizó para otro cambio.", 409)
        return existing.new_enabled, True
    # The conditional UPDATE is the serialization point. PostgreSQL rechecks
    # the predicate after waiting on the row lock; SQLite serializes writers.
    # A changed row necessarily held the opposite boolean immediately before
    # this operation, so the audit never depends on stale ORM identity state.
    changed = db.session.query(Business).filter(
        Business.id == business.id,
        Business.referrals_enabled.isnot(enabled),
    ).update({Business.referrals_enabled: enabled}, synchronize_session=False)
    previous = (not enabled) if changed else enabled
    db.session.expire(business, ["referrals_enabled"])
    db.session.add(ReferralProgramAudit(
        business_id=business.id, actor_type=actor_type, actor_id=actor_id,
        previous_enabled=previous, new_enabled=enabled, reason=reason,
        request_id=request_id, request_fingerprint=fingerprint,
    ))
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        winner = ReferralProgramAudit.query.filter_by(
            business_id=business.id, request_id=request_id,
        ).first()
        if winner and winner.request_fingerprint == fingerprint:
            return winner.new_enabled, True
        raise ReferralProgramError("El cambio entró en conflicto con otra solicitud.", 409) from exc
    return enabled, False
