"""Referral participation and invitation resolution without economic effects."""

import hashlib
import json
import secrets
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Business, Customer, Promoter, ReferralAttribution, ReferralEligibility,
    ReferralParticipant, ReferralParticipantAudit, Staff,
)
from app.permissions import has_permission


class ReferralParticipantError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _utcnow():
    return datetime.now(timezone.utc)


def _snapshot(participant):
    return json.dumps({
        "participant_type": participant.participant_type,
        "is_enabled": participant.is_enabled,
        "revoked": participant.revoked_at is not None,
        "consent_version": participant.consent_version,
        "consented_at": participant.consented_at.isoformat() if participant.consented_at else None,
    }, sort_keys=True, separators=(",", ":"))


def _request(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise ReferralParticipantError("Se requiere un identificador de solicitud válido.")
    return value.strip()


def _reason(value):
    value = value.strip() if isinstance(value, str) else ""
    if not value or len(value) > 500:
        raise ReferralParticipantError("Indica un motivo de hasta 500 caracteres.")
    return value


def _mutation_fingerprint(*, business_id, subject_type, subject_id, actor_type,
                          actor_id, action, reason, consent_version=None,
                          consent_evidence_hash=None):
    return hashlib.sha256(json.dumps({
        "business_id": business_id,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "actor_type": actor_type,
        "actor_id": actor_id,
        "action": action,
        "reason": reason,
        "consent_version": consent_version,
        "consent_evidence_hash": consent_evidence_hash,
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _historical_state(audit):
    try:
        state = json.loads(audit.new_state)
    except (TypeError, ValueError):
        raise ReferralParticipantError("El resultado histórico no es válido.", 409)
    if not isinstance(state, dict) or not isinstance(state.get("is_enabled"), bool):
        raise ReferralParticipantError("El resultado histórico no es válido.", 409)
    return state


def _audit(participant, actor_type, actor_id, action, reason, request_id, previous,
           fingerprint):
    state = _snapshot(participant)
    db.session.add(ReferralParticipantAudit(
        business_id=participant.business_id, participant_id=participant.id,
        action=action, actor_type=actor_type, actor_id=actor_id, reason=reason,
        previous_state=previous, new_state=state, request_id=request_id,
        request_fingerprint=fingerprint,
    ))


def ensure_external_promoter(promoter):
    participant = ReferralParticipant.query.filter_by(
        business_id=promoter.business_id, promoter_id=promoter.id,
    ).first()
    if participant:
        return participant
    participant = ReferralParticipant(
        business_id=promoter.business_id, participant_type="external_promoter",
        promoter_id=promoter.id, invitation_code=promoter.referral_code,
        is_enabled=True,
    )
    db.session.add(participant)
    db.session.flush()
    return participant


def configure_staff_referrals(*, business, target_staff_id, enabled, actor,
                              actor_type="staff", reason, request_id):
    request_id, reason = _request(request_id), _reason(reason)
    action = "staff_referrals_enabled" if enabled else "staff_referrals_disabled"
    fingerprint = _mutation_fingerprint(
        business_id=business.id, subject_type="staff", subject_id=target_staff_id,
        actor_type=actor_type, actor_id=actor.id, action=action, reason=reason,
    )
    existing = ReferralParticipantAudit.query.filter_by(
        business_id=business.id, request_id=request_id,
    ).first()
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise ReferralParticipantError("El identificador ya se utilizó para otro cambio.", 409)
        return (
            db.session.get(ReferralParticipant, existing.participant_id), True,
            _historical_state(existing),
        )
    if actor_type == "staff" and actor.id == target_staff_id:
        raise ReferralParticipantError("No puedes habilitar tus propios referidos.", 403)
    target = Staff.query.filter_by(id=target_staff_id, business_id=business.id).first()
    if not target:
        raise ReferralParticipantError("Empleado no encontrado.", 404)
    if enabled and (not target.is_active or not has_permission(target, "refer_customers")):
        raise ReferralParticipantError(
            "El empleado debe estar activo y tener el permiso refer_customers.", 409,
        )
    participant = ReferralParticipant.query.filter_by(
        business_id=business.id, staff_id=target.id,
    ).first()
    if not participant:
        participant = ReferralParticipant(
            business_id=business.id, participant_type="staff", staff_id=target.id,
            invitation_code=secrets.token_urlsafe(24), is_enabled=False,
        )
        db.session.add(participant)
        db.session.flush()
    previous = _snapshot(participant)
    participant.is_enabled = bool(enabled)
    participant.revoked_at = None if enabled else _utcnow()
    _audit(participant, actor_type, actor.id, action, reason, request_id, previous, fingerprint)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        winner = ReferralParticipantAudit.query.filter_by(
            business_id=business.id, request_id=request_id,
        ).first()
        if winner and winner.request_fingerprint == fingerprint:
            return (
                db.session.get(ReferralParticipant, winner.participant_id), True,
                _historical_state(winner),
            )
        raise ReferralParticipantError("El cambio entró en conflicto con otra solicitud.", 409) from exc
    return participant, False, json.loads(_snapshot(participant))


def configure_customer_ambassador(*, customer, enabled, consent_version,
                                  consent_evidence, actor_type, actor_id,
                                  reason, request_id, authorization_verified=False):
    """Internal-only contract. Public callers must provide a future strong authorization."""
    if not authorization_verified:
        raise ReferralParticipantError("La emisión pública requiere autorización del titular.", 403)
    request_id, reason = _request(request_id), _reason(reason)
    action = "customer_ambassador_consented" if enabled else "customer_ambassador_revoked"
    normalized_version = None
    evidence_hash = None
    if enabled:
        if not isinstance(consent_version, str) or not consent_version.strip():
            raise ReferralParticipantError("Se requiere la versión del consentimiento.")
        if not isinstance(consent_evidence, str) or len(consent_evidence) < 16:
            raise ReferralParticipantError("Se requiere evidencia de consentimiento válida.")
        normalized_version = consent_version.strip()[:32]
        evidence_hash = hashlib.sha256(consent_evidence.encode()).hexdigest()
    fingerprint = _mutation_fingerprint(
        business_id=customer.business_id, subject_type="customer",
        subject_id=customer.id, actor_type=actor_type, actor_id=actor_id,
        action=action, reason=reason, consent_version=normalized_version,
        consent_evidence_hash=evidence_hash,
    )
    existing = ReferralParticipantAudit.query.filter_by(
        business_id=customer.business_id, request_id=request_id,
    ).first()
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise ReferralParticipantError("El identificador ya se utilizó para otro cambio.", 409)
        return (
            db.session.get(ReferralParticipant, existing.participant_id), True,
            _historical_state(existing),
        )
    participant = ReferralParticipant.query.filter_by(
        business_id=customer.business_id, customer_id=customer.id,
    ).first()
    if not participant:
        if not enabled:
            raise ReferralParticipantError("La participación no existe.", 404)
        participant = ReferralParticipant(
            business_id=customer.business_id, participant_type="customer_ambassador",
            customer_id=customer.id, invitation_code=secrets.token_urlsafe(24),
            is_enabled=False,
        )
        db.session.add(participant)
    previous = _snapshot(participant) if participant.id else None
    if enabled:
        participant.consent_version = normalized_version
        participant.consented_at = _utcnow()
        participant.consent_evidence_hash = evidence_hash
        participant.is_enabled = True
        participant.revoked_at = None
    else:
        participant.is_enabled = False
        participant.revoked_at = _utcnow()
    db.session.flush()
    _audit(participant, actor_type, actor_id, action, reason, request_id, previous, fingerprint)
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        winner = ReferralParticipantAudit.query.filter_by(
            business_id=customer.business_id, request_id=request_id,
        ).first()
        if winner and winner.request_fingerprint == fingerprint:
            return (
                db.session.get(ReferralParticipant, winner.participant_id), True,
                _historical_state(winner),
            )
        raise ReferralParticipantError("El cambio entró en conflicto con otra solicitud.", 409) from exc
    return participant, False, json.loads(_snapshot(participant))


def ensure_customer_invitation(customer):
    """Return the customer's referral identity, creating an inactive shell.

    The shell only carries a random public attribution identifier. It grants no
    account access and cannot resolve as a referral until explicit consent has
    enabled it. The caller owns the surrounding transaction.
    """
    participant = ReferralParticipant.query.filter_by(
        business_id=customer.business_id, customer_id=customer.id,
    ).first()
    if participant:
        return participant
    participant = ReferralParticipant(
        business_id=customer.business_id,
        participant_type="customer_ambassador",
        customer_id=customer.id,
        invitation_code=secrets.token_urlsafe(24),
        is_enabled=False,
    )
    db.session.add(participant)
    db.session.flush()
    return participant


def ensure_customer_invitation_persisted(customer):
    """Idempotently provision the non-privileged Wallet invitation identity."""
    try:
        participant = ensure_customer_invitation(customer)
        db.session.commit()
        return participant
    except IntegrityError:
        db.session.rollback()
        participant = ReferralParticipant.query.filter_by(
            business_id=customer.business_id, customer_id=customer.id,
        ).first()
        if participant:
            return participant
        raise


def prepare_customer_invitation_consent(*, customer, consent_version, consent_text,
                                        evidence_context, request_id):
    """Create and explicitly enable a new customer's invitation in caller transaction."""
    participant = ensure_customer_invitation(customer)
    if participant.consented_at or participant.revoked_at:
        raise ReferralParticipantError("La participación ya tiene historial.", 409)
    version = consent_version.strip() if isinstance(consent_version, str) else ""
    text = consent_text.strip() if isinstance(consent_text, str) else ""
    if not version or len(version) > 32 or not text:
        raise ReferralParticipantError("El consentimiento configurado no es válido.", 500)
    evidence_hash = hashlib.sha256(json.dumps({
        "version": version,
        "text_hash": hashlib.sha256(text.encode()).hexdigest(),
        "context": str(evidence_context),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = _snapshot(participant)
    participant.consent_version = version
    participant.consented_at = _utcnow()
    participant.consent_evidence_hash = evidence_hash
    participant.is_enabled = True
    fingerprint = _mutation_fingerprint(
        business_id=customer.business_id, subject_type="customer",
        subject_id=customer.id, actor_type="registration_context",
        actor_id=customer.id, action="customer_ambassador_consented",
        reason="Aceptación opcional durante el registro.",
        consent_version=version, consent_evidence_hash=evidence_hash,
    )
    _audit(
        participant, "registration_context", customer.id,
        "customer_ambassador_consented", "Aceptación opcional durante el registro.",
        _request(request_id), previous, fingerprint,
    )
    return participant


def consent_customer_invitation(*, participant, consent_version, consent_text,
                                evidence_context, request_id):
    """Enable a never-consented public invitation without authenticating an account.

    Possession of the Wallet link is deliberately only a low-assurance consent
    context. This operation cannot revoke participation or authorize any private
    customer/economic operation.
    """
    request_id = _request(request_id)
    if participant.participant_type != "customer_ambassador" or not participant.customer:
        raise ReferralParticipantError("La invitación no es válida.", 404)
    version = consent_version.strip() if isinstance(consent_version, str) else ""
    text = consent_text.strip() if isinstance(consent_text, str) else ""
    if not version or len(version) > 32 or not text:
        raise ReferralParticipantError("El consentimiento configurado no es válido.", 500)
    evidence_hash = hashlib.sha256(json.dumps({
        "version": version,
        "text_hash": hashlib.sha256(text.encode()).hexdigest(),
        "context": str(evidence_context),
    }, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    fingerprint = _mutation_fingerprint(
        business_id=participant.business_id,
        subject_type="customer",
        subject_id=participant.customer_id,
        actor_type="public_wallet_context",
        actor_id=participant.customer_id,
        action="customer_ambassador_consented",
        reason="Aceptación expresa desde la invitación incluida en Wallet.",
        consent_version=version,
        consent_evidence_hash=evidence_hash,
    )
    existing = ReferralParticipantAudit.query.filter_by(
        business_id=participant.business_id, request_id=request_id,
    ).first()
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise ReferralParticipantError("El identificador ya se utilizó para otro cambio.", 409)
        return participant, True, _historical_state(existing)
    if participant.revoked_at is not None or participant.consented_at is not None:
        raise ReferralParticipantError("La invitación no puede activarse.", 409)
    business = participant.customer.business
    if not business.is_active or not business.referrals_enabled:
        raise ReferralParticipantError("El programa de referidos no está disponible.", 409)
    previous = _snapshot(participant)
    participant.consent_version = version
    participant.consented_at = _utcnow()
    participant.consent_evidence_hash = evidence_hash
    participant.is_enabled = True
    participant.revoked_at = None
    _audit(
        participant, "public_wallet_context", participant.customer_id,
        "customer_ambassador_consented",
        "Aceptación expresa desde la invitación incluida en Wallet.",
        request_id, previous, fingerprint,
    )
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        winner = ReferralParticipantAudit.query.filter_by(
            business_id=participant.business_id, request_id=request_id,
        ).first()
        if winner and winner.request_fingerprint == fingerprint:
            return (
                db.session.get(ReferralParticipant, winner.participant_id), True,
                _historical_state(winner),
            )
        raise ReferralParticipantError(
            "La activación entró en conflicto con otra solicitud.", 409,
        ) from exc
    return participant, False, json.loads(_snapshot(participant))


def resolve_invitation(*, business_id, code):
    # Newly issued codes are high-entropy and at least 20 characters. Existing
    # promoter codes may be shorter and remain valid after the migration.
    if not isinstance(code, str) or not 1 <= len(code) <= 48:
        return None
    business = db.session.get(Business, business_id)
    if not business or not business.referrals_enabled:
        return None
    participant = ReferralParticipant.query.filter_by(
        business_id=business_id, invitation_code=code, is_enabled=True,
    ).first()
    return participant if participant and participant.currently_eligible() else None


def create_pending_attribution(*, business, customer, participant):
    if participant.business_id != business.id or customer.business_id != business.id:
        raise ReferralParticipantError("La invitación no corresponde al negocio.", 409)
    if not business.referrals_enabled:
        return None
    if not participant.currently_eligible():
        return None
    if participant.participant_type == "customer_ambassador" and participant.customer_id == customer.id:
        return None
    attribution = ReferralAttribution(
        business_id=business.id, participant_id=participant.id,
        promoter_id=participant.promoter_id, customer_id=customer.id,
    )
    db.session.add(attribution)
    db.session.flush()
    eligibility = ReferralEligibility(
        business_id=business.id, attribution_id=attribution.id,
        participant_id=participant.id, promoter_id=participant.promoter_id,
        customer_id=customer.id, status="pending",
    )
    db.session.add(eligibility)
    return attribution


def staff_can_issue(participant):
    return bool(
        participant and participant.participant_type == "staff"
        and participant.staff and participant.staff.is_active
        and participant.staff.business.referrals_enabled
        and has_permission(participant.staff, "refer_customers")
        and participant.currently_eligible()
    )
