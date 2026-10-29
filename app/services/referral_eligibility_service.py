"""Manual, auditable referral eligibility decisions without reward side effects."""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    ReferralEligibility, ReferralEligibilityDecision, Transaction,
)


class ReferralEligibilityError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class ReferralDecisionResult:
    eligibility: ReferralEligibility
    replayed: bool = False


_TRANSITIONS = {
    "pending": {"in_review", "verified", "rejected"},
    "in_review": {"verified", "rejected"},
    "verified": {"revoked"},
}


def _digest(value, purpose):
    key = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(key, f"{purpose}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _fingerprint(eligibility_id, to_status, transaction_id, reference, reason):
    canonical = json.dumps({
        "eligibility_id": eligibility_id,
        "to_status": to_status,
        "transaction_id": transaction_id,
        "reference_hash": _digest(reference, "referral-evidence") if reference else None,
        "reason": reason,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def decide_referral_eligibility(*, business, actor, eligibility_id, to_status,
                                transaction_id, evidence_reference, reason, request_id):
    """Apply one conditional transition and append its audit record atomically."""
    if not business.is_active:
        raise ReferralEligibilityError("El negocio está suspendido.", 403)
    if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 64:
        raise ReferralEligibilityError("Se requiere un identificador de solicitud válido.")
    if to_status not in {"in_review", "verified", "rejected", "revoked"}:
        raise ReferralEligibilityError("Estado de revisión inválido.")
    reason = reason.strip() if isinstance(reason, str) else ""
    reference = evidence_reference.strip() if isinstance(evidence_reference, str) else ""
    if not reason or len(reason) > 500:
        raise ReferralEligibilityError("Indica un motivo de hasta 500 caracteres.")
    request_id = request_id.strip()
    fingerprint = _fingerprint(eligibility_id, to_status, transaction_id, reference, reason)

    previous = ReferralEligibilityDecision.query.filter_by(
        business_id=business.id, request_id=request_id,
    ).first()
    if previous:
        if previous.request_fingerprint != fingerprint:
            raise ReferralEligibilityError("El identificador ya se usó para otra decisión.", 409)
        return ReferralDecisionResult(db.session.get(ReferralEligibility, previous.eligibility_id), True)

    eligibility = ReferralEligibility.query.filter_by(
        id=eligibility_id, business_id=business.id,
    ).first()
    if not eligibility:
        raise ReferralEligibilityError("Atribución no encontrada.", 404)
    if to_status not in _TRANSITIONS.get(eligibility.status, set()):
        raise ReferralEligibilityError("La transición de estado no está permitida.", 409)
    if not eligibility.attribution or (
        eligibility.attribution.promoter_id != eligibility.promoter_id
        or eligibility.attribution.customer_id != eligibility.customer_id
    ):
        raise ReferralEligibilityError("La atribución no es consistente.", 409)

    transaction = None
    evidence_hash = None
    if to_status == "verified":
        if not reference or len(reference) > 200:
            raise ReferralEligibilityError("Se requiere una referencia de evidencia válida.")
        if not isinstance(transaction_id, int) or isinstance(transaction_id, bool):
            raise ReferralEligibilityError("Se requiere una compra válida.")
        transaction = Transaction.query.filter_by(
            id=transaction_id, business_id=business.id,
            customer_id=eligibility.customer_id, type="earn",
        ).first()
        if not transaction or transaction.purchase_amount <= 0:
            raise ReferralEligibilityError("La compra indicada no es elegible.")
        if not eligibility.attribution.promoter.is_active:
            raise ReferralEligibilityError("El promotor está suspendido.", 409)
        evidence_hash = _digest(reference, "referral-evidence")
    elif transaction_id is not None or reference:
        raise ReferralEligibilityError("La evidencia de compra solo se admite al verificar.")

    now = datetime.now(timezone.utc)
    old_status = eligibility.status
    values = {
        "status": to_status, "reviewed_by_staff_id": actor.id,
        "reviewed_at": now, "updated_at": now,
        "version": eligibility.version + 1,
    }
    if to_status == "verified":
        values.update(source_transaction_id=transaction.id, evidence_reference_hash=evidence_hash)

    try:
        changed = db.session.execute(
            update(ReferralEligibility).where(
                ReferralEligibility.id == eligibility.id,
                ReferralEligibility.business_id == business.id,
                ReferralEligibility.version == eligibility.version,
                ReferralEligibility.status == old_status,
            ).values(**values)
        )
        if changed.rowcount != 1:
            db.session.rollback()
            winner = ReferralEligibilityDecision.query.filter_by(
                business_id=business.id, request_id=request_id,
            ).first()
            if winner and winner.request_fingerprint == fingerprint:
                return ReferralDecisionResult(
                    db.session.get(ReferralEligibility, winner.eligibility_id), True
                )
            raise ReferralEligibilityError("La revisión cambió en otra solicitud.", 409)
        db.session.add(ReferralEligibilityDecision(
            business_id=business.id, eligibility_id=eligibility.id,
            actor_staff_id=actor.id, from_status=old_status, to_status=to_status,
            source_transaction_id=transaction.id if transaction else None,
            evidence_reference_hash=evidence_hash, reason=reason,
            request_id=request_id, request_fingerprint=fingerprint,
        ))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        winner = ReferralEligibilityDecision.query.filter_by(
            business_id=business.id, request_id=request_id,
        ).first()
        if winner and winner.request_fingerprint == fingerprint:
            return ReferralDecisionResult(db.session.get(ReferralEligibility, winner.eligibility_id), True)
        raise ReferralEligibilityError("La evidencia o la decisión ya fue utilizada.", 409)

    return ReferralDecisionResult(db.session.get(ReferralEligibility, eligibility.id))

