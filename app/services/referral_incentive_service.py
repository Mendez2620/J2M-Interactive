"""Disabled-by-design referral incentive configuration and dry-run evaluation."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    ReferralEligibility, ReferralIncentiveRecord, ReferralIncentiveRule,
    ReferralIncentiveRuleAudit,
)


class ReferralIncentiveError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class IncentivePreview:
    outcome: str
    reason: str
    eligibility_id: int
    rule_id: int
    rule_version: int
    beneficiary_type: str
    beneficiary_id: int | None
    dry_run: bool = True


@dataclass(frozen=True)
class RuleMutationResult:
    rule: ReferralIncentiveRule
    replayed: bool
    response_snapshot: str
    response_version: int


_RULE_FIELDS = {
    "beneficiary_type", "loyalty_program", "reward_type", "reward_amount",
    "eligible_condition", "period_limit", "period_unit", "valid_from",
    "valid_until", "status",
}
_BENEFICIARIES = {"external_promoter", "staff", "customer_ambassador"}
_PROGRAMS = {"points", "stamps"}
_STATUSES = {"draft", "disabled"}


def _utcnow():
    return datetime.now(timezone.utc)


def _parse_datetime(value, field):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ReferralIncentiveError(f"{field} debe ser una fecha ISO 8601.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReferralIncentiveError(f"{field} debe ser una fecha ISO 8601.") from exc
    if parsed.tzinfo is None:
        raise ReferralIncentiveError(f"{field} debe incluir zona horaria.")
    return parsed.astimezone(timezone.utc)


def _normalize_rule_payload(business, data, *, partial=False):
    if not isinstance(data, dict) or set(data) - (_RULE_FIELDS | {"reason", "version", "request_id"}):
        raise ReferralIncentiveError("Configuración de incentivo inválida.")
    values = {}
    required = set(data) & _RULE_FIELDS
    core = {
        "beneficiary_type", "loyalty_program", "reward_type", "reward_amount",
        "eligible_condition",
    }
    if not partial and not core <= set(data):
        raise ReferralIncentiveError("Faltan campos obligatorios de la regla.")
    if "beneficiary_type" in required:
        value = data.get("beneficiary_type")
        if value not in _BENEFICIARIES:
            raise ReferralIncentiveError("Tipo de beneficiario inválido.")
        values["beneficiary_type"] = value
    if "loyalty_program" in required:
        value = data.get("loyalty_program")
        if value not in _PROGRAMS or value != business.loyalty_type:
            raise ReferralIncentiveError("La regla debe usar el programa de lealtad del negocio.")
        values["loyalty_program"] = value
    if "reward_type" in required:
        value = data.get("reward_type")
        if value not in _PROGRAMS or value != business.loyalty_type:
            raise ReferralIncentiveError("La recompensa debe usar la unidad del programa del negocio.")
        values["reward_type"] = value
    if "reward_amount" in required:
        raw = data.get("reward_amount")
        if isinstance(raw, bool):
            raise ReferralIncentiveError("La cantidad del incentivo es inválida.")
        try:
            amount = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            raise ReferralIncentiveError("La cantidad del incentivo es inválida.")
        if not amount.is_finite() or amount <= 0 or amount.as_tuple().exponent < -6:
            raise ReferralIncentiveError("La cantidad debe ser positiva y usar hasta seis decimales.")
        if business.loyalty_type == "stamps" and amount != amount.to_integral_value():
            raise ReferralIncentiveError("Los sellos deben configurarse como una cantidad entera.")
        values["reward_amount"] = amount
    if "eligible_condition" in required:
        if data.get("eligible_condition") != "first_verified_purchase":
            raise ReferralIncentiveError("Solo se admite la primera compra verificada.")
        values["eligible_condition"] = "first_verified_purchase"
    if "status" in required:
        if data.get("status") not in _STATUSES:
            raise ReferralIncentiveError("La regla solo puede quedar en borrador o deshabilitada.")
        values["status"] = data["status"]
    elif not partial:
        values["status"] = "disabled"
    if partial and (("period_limit" in required) != ("period_unit" in required)):
        raise ReferralIncentiveError(
            "period_limit y period_unit deben actualizarse juntos."
        )
    if {"period_limit", "period_unit"} & required:
        limit, unit = data.get("period_limit"), data.get("period_unit")
        if limit is None and unit is None:
            values.update(period_limit=None, period_unit=None)
        elif not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 or unit not in {"day", "month"}:
            raise ReferralIncentiveError("El límite requiere cantidad positiva y periodo día o mes.")
        else:
            values.update(period_limit=limit, period_unit=unit)
    elif not partial:
        values.update(period_limit=None, period_unit=None)
    if "valid_from" in required:
        values["valid_from"] = _parse_datetime(data.get("valid_from"), "valid_from")
    if "valid_until" in required:
        values["valid_until"] = _parse_datetime(data.get("valid_until"), "valid_until")
    start = values.get("valid_from", data.get("valid_from") if partial else None)
    end = values.get("valid_until", data.get("valid_until") if partial else None)
    if isinstance(start, datetime) and isinstance(end, datetime) and end <= start:
        raise ReferralIncentiveError("La vigencia final debe ser posterior a la inicial.")
    return values


def _snapshot(rule, overrides=None):
    values = {
        "beneficiary_type": rule.beneficiary_type,
        "loyalty_program": rule.loyalty_program,
        "reward_type": rule.reward_type,
        "reward_amount": str(rule.reward_amount),
        "eligible_condition": rule.eligible_condition,
        "period_limit": rule.period_limit,
        "period_unit": rule.period_unit,
        "valid_from": rule.valid_from.isoformat() if rule.valid_from else None,
        "valid_until": rule.valid_until.isoformat() if rule.valid_until else None,
        "status": rule.status,
        "version": rule.version,
    }
    for key, value in (overrides or {}).items():
        values[key] = value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, Decimal) else value
    return json.dumps(values, sort_keys=True, separators=(",", ":"))


def _fingerprint(action, rule_id, snapshot, reason):
    raw = json.dumps({"action": action, "rule_id": rule_id, "snapshot": snapshot, "reason": reason},
                     sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _update_fingerprint(rule_id, expected_version, values, reason):
    canonical_values = {
        key: value.isoformat() if isinstance(value, datetime)
        else str(value) if isinstance(value, Decimal) else value
        for key, value in values.items()
    }
    raw = json.dumps({
        "action": "updated", "rule_id": rule_id,
        "expected_version": expected_version, "values": canonical_values,
        "reason": reason,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _request_id(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 64:
        raise ReferralIncentiveError("Se requiere un identificador de solicitud válido.")
    return value.strip()


def _reason(value):
    value = value.strip() if isinstance(value, str) else ""
    if not value or len(value) > 500:
        raise ReferralIncentiveError("Indica un motivo de hasta 500 caracteres.")
    return value


def create_rule(*, business, actor, data, request_id):
    if not business.is_active:
        raise ReferralIncentiveError("El negocio está suspendido.", 403)
    request_id, reason = _request_id(request_id), _reason(data.get("reason"))
    values = _normalize_rule_payload(business, data)
    rule = ReferralIncentiveRule(
        business_id=business.id, **values, version=1,
        created_by_type="staff", created_by_id=actor.id,
        updated_by_type="staff", updated_by_id=actor.id,
    )
    candidate_snapshot = _snapshot(rule)
    previous = ReferralIncentiveRuleAudit.query.filter_by(
        business_id=business.id, request_id=request_id
    ).first()
    if previous:
        if previous.new_snapshot != candidate_snapshot or previous.reason != reason:
            raise ReferralIncentiveError("El identificador de solicitud ya fue utilizado.", 409)
        return RuleMutationResult(
            db.session.get(ReferralIncentiveRule, previous.rule_id), True,
            previous.new_snapshot, previous.rule_version,
        )
    db.session.add(rule)
    try:
        db.session.flush()
        snapshot = _snapshot(rule)
        db.session.add(ReferralIncentiveRuleAudit(
            business_id=business.id, rule_id=rule.id, rule_version=1,
            action="created", actor_type="staff", actor_id=actor.id,
            previous_snapshot=None, new_snapshot=snapshot, reason=reason,
            request_id=request_id,
            request_fingerprint=_fingerprint("created", rule.id, snapshot, reason),
        ))
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        winner = ReferralIncentiveRuleAudit.query.filter_by(
            business_id=business.id, request_id=request_id
        ).first()
        if winner and winner.new_snapshot == candidate_snapshot and winner.reason == reason:
            return RuleMutationResult(
                db.session.get(ReferralIncentiveRule, winner.rule_id), True,
                winner.new_snapshot, winner.rule_version,
            )
        raise ReferralIncentiveError("No fue posible crear la regla sin duplicados.", 409) from exc
    return RuleMutationResult(rule, False, snapshot, 1)


def update_rule(*, business, actor, rule_id, data, request_id):
    if not business.is_active:
        raise ReferralIncentiveError("El negocio está suspendido.", 403)
    request_id, reason = _request_id(request_id), _reason(data.get("reason"))
    if not isinstance(data.get("version"), int) or isinstance(data.get("version"), bool):
        raise ReferralIncentiveError("Se requiere la versión vigente de la regla.")
    values = _normalize_rule_payload(business, data, partial=True)
    if not values:
        raise ReferralIncentiveError("No se recibieron cambios de configuración.")
    expected_version = data["version"]
    request_fingerprint = _update_fingerprint(
        rule_id, expected_version, values, reason
    )
    previous = ReferralIncentiveRuleAudit.query.filter_by(
        business_id=business.id, request_id=request_id
    ).first()
    if previous:
        if previous.request_fingerprint != request_fingerprint:
            raise ReferralIncentiveError("El identificador ya se utilizó para otro cambio.", 409)
        return RuleMutationResult(
            db.session.get(ReferralIncentiveRule, previous.rule_id), True,
            previous.new_snapshot, previous.rule_version,
        )
    rule = ReferralIncentiveRule.query.filter_by(id=rule_id, business_id=business.id).first()
    if not rule:
        raise ReferralIncentiveError("Regla no encontrada.", 404)
    if "valid_from" not in values:
        values["valid_from"] = rule.valid_from
    if "valid_until" not in values:
        values["valid_until"] = rule.valid_until
    if values["valid_from"] and values["valid_until"] and values["valid_until"] <= values["valid_from"]:
        raise ReferralIncentiveError("La vigencia final debe ser posterior a la inicial.")
    old_snapshot = _snapshot(rule)
    new_version = rule.version + 1
    preview_snapshot = _snapshot(rule, {**values, "version": new_version})
    changed = db.session.execute(update(ReferralIncentiveRule).where(
        ReferralIncentiveRule.id == rule.id,
        ReferralIncentiveRule.business_id == business.id,
        ReferralIncentiveRule.version == expected_version,
    ).values(**values, version=new_version, updated_by_type="staff", updated_by_id=actor.id,
             updated_at=_utcnow()))
    if changed.rowcount != 1:
        db.session.rollback()
        winner = ReferralIncentiveRuleAudit.query.filter_by(
            business_id=business.id, request_id=request_id
        ).first()
        if winner and winner.request_fingerprint == request_fingerprint:
            return RuleMutationResult(
                db.session.get(ReferralIncentiveRule, winner.rule_id), True,
                winner.new_snapshot, winner.rule_version,
            )
        raise ReferralIncentiveError("La regla cambió en otra solicitud.", 409)
    db.session.add(ReferralIncentiveRuleAudit(
        business_id=business.id, rule_id=rule.id, rule_version=new_version,
        action="updated", actor_type="staff", actor_id=actor.id,
        previous_snapshot=old_snapshot, new_snapshot=preview_snapshot, reason=reason,
        request_id=request_id, request_fingerprint=request_fingerprint,
    ))
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        winner = ReferralIncentiveRuleAudit.query.filter_by(
            business_id=business.id, request_id=request_id
        ).first()
        if winner and winner.request_fingerprint == request_fingerprint:
            return RuleMutationResult(
                db.session.get(ReferralIncentiveRule, winner.rule_id), True,
                winner.new_snapshot, winner.rule_version,
            )
        raise ReferralIncentiveError("La regla cambió en otra solicitud.", 409) from exc
    return RuleMutationResult(
        db.session.get(ReferralIncentiveRule, rule.id), False,
        preview_snapshot, new_version,
    )


def preview_incentive(*, business, eligibility_id, rule_id, now=None):
    """Evaluate without inserts, balance changes, ledger entries, outbox jobs or payouts."""
    now = now or _utcnow()
    rule = ReferralIncentiveRule.query.filter_by(id=rule_id, business_id=business.id).first()
    eligibility = ReferralEligibility.query.filter_by(
        id=eligibility_id, business_id=business.id
    ).first()
    if not rule or not eligibility:
        raise ReferralIncentiveError("Regla o elegibilidad no encontrada.", 404)
    beneficiary_id = eligibility.promoter_id if rule.beneficiary_type == "external_promoter" else None

    def result(outcome, reason):
        return IncentivePreview(
            outcome, reason, eligibility.id, rule.id, rule.version,
            rule.beneficiary_type, beneficiary_id,
        )

    if rule.status == "disabled":
        return result("rule_disabled", "La regla está deshabilitada y no puede generar incentivos.")
    if eligibility.status == "revoked":
        return result("revoked", "La verificación fue revocada.")
    if eligibility.status in {"pending", "in_review"}:
        return result("pending_verification", "La primera compra todavía no está verificada.")
    if eligibility.status != "verified":
        return result("not_eligible", "La elegibilidad fue rechazada o no es válida.")
    if not business.is_active:
        return result("not_eligible", "El negocio está suspendido.")
    if rule.loyalty_program != business.loyalty_type or rule.reward_type != business.loyalty_type:
        return result("not_eligible", "La regla no corresponde al programa vigente.")
    if rule.beneficiary_type != "external_promoter":
        return result("not_eligible", "Este tipo de beneficiario se habilitará en 8C.2B.")
    if not eligibility.attribution or not eligibility.attribution.promoter.is_active:
        return result("not_eligible", "El promotor o la atribución no están activos.")
    if not eligibility.source_transaction_id or not eligibility.evidence_reference_hash:
        return result("not_eligible", "Falta evidencia auditada de la primera compra.")
    if rule.valid_from and now < rule.valid_from.replace(tzinfo=timezone.utc):
        return result("not_eligible", "La regla todavía no está vigente.")
    if rule.valid_until and now >= rule.valid_until.replace(tzinfo=timezone.utc):
        return result("not_eligible", "La regla ya no está vigente.")
    duplicate = ReferralIncentiveRecord.query.filter_by(
        business_id=business.id,
        source_transaction_id=eligibility.source_transaction_id,
        beneficiary_type=rule.beneficiary_type,
    ).first()
    if duplicate:
        return result("conflict_or_duplicate", "Ya existe un registro para este evento y regla.")
    if rule.period_limit:
        return result(
            "not_eligible",
            "El límite calendario permanece deshabilitado hasta configurar la zona horaria del negocio.",
        )
    return result("eligible_for_proposal", "Cumple las condiciones para una propuesta en modo DRY-RUN.")
