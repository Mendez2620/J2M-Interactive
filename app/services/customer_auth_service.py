import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import current_app, session
from sqlalchemy.exc import IntegrityError
from webauthn import (
    base64url_to_bytes, generate_authentication_options,
    generate_registration_options, options_to_json,
    verify_authentication_response, verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
    ResidentKeyRequirement, UserVerificationRequirement,
)

from app.extensions import db
from app.models import (
    Business, Customer, CustomerAuthAudit, CustomerAuthChallenge,
    CustomerCredential, CustomerLinkingEnrollment, CustomerSession,
    PromoterSession, Staff, StaffSession,
)
from app.permissions import has_permission


class CustomerAuthError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _pairing_hash(enrollment_id, value):
    key = current_app.config["SECRET_KEY"].encode()
    return hmac.new(key, f"{enrollment_id}:{value}".encode(), hashlib.sha256).hexdigest()


def _webauthn_context():
    origin = (current_app.config.get("WEBAUTHN_ORIGIN") or current_app.config.get("PUBLIC_BASE_URL") or "").rstrip("/")
    if not origin and current_app.config.get("APP_ENV") != "production":
        origin = "http://localhost"
    parsed = urlparse(origin)
    rp_id = (current_app.config.get("WEBAUTHN_RP_ID") or parsed.hostname or "").strip()
    if not rp_id or parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CustomerAuthError("WebAuthn no está configurado de forma segura.", 503)
    if current_app.config.get("APP_ENV") == "production" and parsed.scheme != "https":
        raise CustomerAuthError("WebAuthn requiere HTTPS en producción.", 503)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise CustomerAuthError("El origen WebAuthn configurado no es válido.", 503)
    if rp_id != parsed.hostname and not parsed.hostname.endswith(f".{rp_id}"):
        raise CustomerAuthError("El RP ID WebAuthn no corresponde al origen configurado.", 503)
    return rp_id, origin


def _audit(customer, action, actor_type, actor_id, reason, enrollment=None, credential=None):
    db.session.add(CustomerAuthAudit(
        business_id=customer.business_id, customer_id=customer.id,
        action=action, actor_type=actor_type, actor_id=actor_id,
        enrollment_id=enrollment.id if enrollment else None,
        credential_id=credential.id if credential else None, reason=reason,
    ))


def create_linking_enrollment(*, staff, customer_id, human_check_note, recovery_mode=False):
    note = human_check_note.strip() if isinstance(human_check_note, str) else ""
    if (not staff.is_active or not staff.web_access_enabled or not staff.business
            or not staff.business.is_active or not staff.business.web_access_enabled
            or not has_permission(staff, "authorize_customer_identity")):
        raise CustomerAuthError("No tienes autorización para vincular identidades.", 403)
    if not note or len(note) > 500:
        raise CustomerAuthError("Documenta la comprobación humana realizada.")
    customer = Customer.query.filter_by(id=customer_id, business_id=staff.business_id).first()
    if not customer:
        raise CustomerAuthError("Cliente no encontrado.", 404)
    secret = secrets.token_urlsafe(32)
    enrollment = CustomerLinkingEnrollment(
        business_id=staff.business_id, customer_id=customer.id,
        authorized_by_staff_id=staff.id, secret_hash=_sha(secret),
        recovery_mode=bool(recovery_mode), human_check_note=note,
        expires_at=_now() + timedelta(seconds=current_app.config["CUSTOMER_LINKING_MAX_AGE"]),
    )
    db.session.add(enrollment)
    db.session.flush()
    _audit(customer, "linking_authorized", "staff", staff.id, note, enrollment=enrollment)
    db.session.commit()
    return enrollment, secret


def claim_linking_enrollment(secret):
    if not isinstance(secret, str) or len(secret) < 32:
        raise CustomerAuthError("La autorización no es válida o expiró.", 404)
    secret_hash = _sha(secret)
    enrollment = CustomerLinkingEnrollment.query.filter_by(secret_hash=secret_hash).first()
    if not enrollment or enrollment.status != "pending" or enrollment.expires_at <= _now():
        raise CustomerAuthError("La autorización no es válida o expiró.", 404)
    customer = Customer.query.filter_by(id=enrollment.customer_id, business_id=enrollment.business_id).first()
    staff = Staff.query.filter_by(id=enrollment.authorized_by_staff_id, business_id=enrollment.business_id).first()
    if not customer or not staff or not staff.business.is_active or not staff.is_active or not has_permission(staff, "authorize_customer_identity"):
        raise CustomerAuthError("La autorización ya no está disponible.", 409)
    browser_verifier = secrets.token_urlsafe(32)
    pairing_code = f"{secrets.randbelow(1000000):06d}"
    claimed_at = _now()
    changed = CustomerLinkingEnrollment.query.filter(
        CustomerLinkingEnrollment.id == enrollment.id,
        CustomerLinkingEnrollment.secret_hash == secret_hash,
        CustomerLinkingEnrollment.status == "pending",
        CustomerLinkingEnrollment.expires_at > claimed_at,
    ).update({
        CustomerLinkingEnrollment.browser_binding_hash: _sha(browser_verifier),
        CustomerLinkingEnrollment.pairing_code_hash: _pairing_hash(enrollment.id, pairing_code),
        CustomerLinkingEnrollment.claimed_at: claimed_at,
        CustomerLinkingEnrollment.status: "claimed",
    }, synchronize_session=False)
    if changed != 1:
        db.session.rollback()
        raise CustomerAuthError("La autorización no es válida o expiró.", 404)
    enrollment = db.session.get(CustomerLinkingEnrollment, enrollment.id)
    _audit(customer, "linking_claimed", "customer_browser", None, "Dispositivo presentado para emparejamiento.", enrollment=enrollment)
    db.session.commit()
    session["customer_linking_id"] = enrollment.id
    session["customer_linking_verifier"] = browser_verifier
    return enrollment, pairing_code


def approve_linking_enrollment(*, staff, enrollment_id, pairing_code):
    enrollment = CustomerLinkingEnrollment.query.filter_by(
        id=enrollment_id, business_id=staff.business_id,
    ).with_for_update().first()
    if not enrollment or enrollment.status != "claimed" or enrollment.expires_at <= _now():
        raise CustomerAuthError("La solicitud no está disponible.", 404)
    if not staff.is_active or not has_permission(staff, "authorize_customer_identity"):
        raise CustomerAuthError("No tienes autorización para confirmar vinculaciones.", 403)
    supplied = _pairing_hash(enrollment.id, str(pairing_code or ""))
    if not hmac.compare_digest(supplied, enrollment.pairing_code_hash or ""):
        raise CustomerAuthError("El código de emparejamiento no coincide.", 409)
    approved_at = _now()
    changed = CustomerLinkingEnrollment.query.filter_by(
        id=enrollment.id, business_id=staff.business_id, status="claimed",
    ).filter(CustomerLinkingEnrollment.expires_at > approved_at).update({
        CustomerLinkingEnrollment.status: "approved",
        CustomerLinkingEnrollment.approved_at: approved_at,
    }, synchronize_session=False)
    if changed != 1:
        db.session.rollback()
        raise CustomerAuthError("La solicitud ya fue confirmada o expiró.", 409)
    enrollment = db.session.get(CustomerLinkingEnrollment, enrollment.id)
    customer = db.session.get(Customer, enrollment.customer_id)
    _audit(customer, "linking_pair_confirmed", "staff", staff.id, "Emparejamiento presencial confirmado.", enrollment=enrollment)
    db.session.commit()
    return enrollment


def _bound_enrollment(lock=False):
    enrollment_id = session.get("customer_linking_id")
    verifier = session.get("customer_linking_verifier")
    query = CustomerLinkingEnrollment.query.filter_by(id=enrollment_id)
    enrollment = query.with_for_update().first() if lock else query.first()
    if not enrollment or not verifier or not enrollment.browser_binding_hash or not hmac.compare_digest(enrollment.browser_binding_hash, _sha(verifier)):
        raise CustomerAuthError("La sesión de vinculación no es válida.", 403)
    if enrollment.expires_at <= _now():
        raise CustomerAuthError("La sesión de vinculación expiró.", 410)
    return enrollment


def registration_options():
    enrollment = _bound_enrollment(lock=True)
    if enrollment.status not in {"approved", "registration_pending"}:
        raise CustomerAuthError("La vinculación aún no fue confirmada por el personal.", 409)
    customer = Customer.query.filter_by(id=enrollment.customer_id, business_id=enrollment.business_id).first()
    existing = CustomerCredential.query.filter_by(
        business_id=enrollment.business_id, customer_id=enrollment.customer_id, is_active=True,
    ).all()
    rp_id, _origin = _webauthn_context()
    challenge = secrets.token_bytes(32)
    options = generate_registration_options(
        rp_id=rp_id, rp_name=current_app.config["WEBAUTHN_RP_NAME"],
        user_id=f"j2m:{customer.business_id}:{customer.id}".encode(),
        user_name=f"customer-{customer.business_id}-{customer.id}",
        user_display_name="Cliente J2M",
        challenge=challenge,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=item.credential_id) for item in existing],
    )
    enrollment.webauthn_challenge = challenge
    enrollment.status = "registration_pending"
    db.session.commit()
    return json.loads(options_to_json(options))


def _create_customer_session(customer, credential):
    now = _now()
    for key in ("manager_session_id", "staff_session_id"):
        prior = db.session.get(StaffSession, session.get(key)) if session.get(key) else None
        if prior and not prior.revoked_at:
            prior.revoked_at = now
    prior_promoter = (
        db.session.get(PromoterSession, session.get("promoter_session_id"))
        if session.get("promoter_session_id") else None
    )
    if prior_promoter and not prior_promoter.revoked_at:
        prior_promoter.revoked_at = now
    session.clear()
    session_id = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    auth_session = CustomerSession(
        id=session_id, business_id=customer.business_id, customer_id=customer.id,
        credential_id=credential.id, verifier_hash=_sha(verifier),
        expires_at=now + timedelta(seconds=current_app.config["CUSTOMER_SESSION_ABSOLUTE_SECONDS"]),
        last_active_at=now,
    )
    db.session.add(auth_session)
    session["customer_session_id"] = session_id
    session["customer_session_verifier"] = verifier
    session["customer_business_id"] = customer.business_id
    return auth_session


def complete_registration(credential_payload):
    enrollment = _bound_enrollment(lock=False)
    if enrollment.status != "registration_pending" or not enrollment.webauthn_challenge:
        raise CustomerAuthError("La ceremonia de registro no está disponible.", 409)
    changed = CustomerLinkingEnrollment.query.filter_by(
        id=enrollment.id, status="registration_pending",
    ).update({CustomerLinkingEnrollment.status: "verifying"}, synchronize_session=False)
    if changed != 1:
        db.session.rollback()
        raise CustomerAuthError("La ceremonia ya está siendo procesada.", 409)
    db.session.commit()
    enrollment = db.session.get(CustomerLinkingEnrollment, enrollment.id)
    try:
        rp_id, origin = _webauthn_context()
        verified = verify_registration_response(
            credential=credential_payload, expected_challenge=enrollment.webauthn_challenge,
            expected_rp_id=rp_id, expected_origin=origin, require_user_verification=True,
        )
    except Exception as exc:
        CustomerLinkingEnrollment.query.filter_by(
            id=enrollment.id, status="verifying",
        ).update({CustomerLinkingEnrollment.status: "registration_pending"}, synchronize_session=False)
        db.session.commit()
        raise CustomerAuthError("No fue posible verificar la passkey.", 400) from exc
    enrollment = CustomerLinkingEnrollment.query.filter_by(
        id=enrollment.id, status="verifying",
    ).with_for_update().first()
    if not enrollment or enrollment.expires_at <= _now():
        db.session.rollback()
        raise CustomerAuthError("La ceremonia expiró antes de completarse.", 409)
    customer = Customer.query.filter_by(id=enrollment.customer_id, business_id=enrollment.business_id).first()
    if enrollment.recovery_mode:
        now = _now()
        CustomerCredential.query.filter_by(business_id=customer.business_id, customer_id=customer.id, is_active=True).update({
            CustomerCredential.is_active: False, CustomerCredential.revoked_at: now,
            CustomerCredential.revoked_reason: "Recuperación asistida autorizada.",
        }, synchronize_session=False)
        CustomerSession.query.filter_by(business_id=customer.business_id, customer_id=customer.id, revoked_at=None).update({
            CustomerSession.revoked_at: now,
        }, synchronize_session=False)
    response = credential_payload.get("response", {}) if isinstance(credential_payload, dict) else {}
    credential = CustomerCredential(
        business_id=customer.business_id, customer_id=customer.id,
        credential_id=verified.credential_id, public_key=verified.credential_public_key,
        sign_count=verified.sign_count,
        device_type=getattr(verified.credential_device_type, "value", str(verified.credential_device_type)),
        backed_up=bool(verified.credential_backed_up),
        transports_json=json.dumps(response.get("transports", [])),
    )
    db.session.add(credential)
    try:
        db.session.flush()
    except IntegrityError as exc:
        db.session.rollback()
        CustomerLinkingEnrollment.query.filter_by(
            id=enrollment.id, status="verifying",
        ).update({CustomerLinkingEnrollment.status: "registration_pending"}, synchronize_session=False)
        db.session.commit()
        raise CustomerAuthError("Esta passkey ya está registrada.", 409) from exc
    enrollment.status = "completed"
    enrollment.consumed_at = _now()
    enrollment.secret_hash = _sha(secrets.token_urlsafe(32))
    enrollment.pairing_code_hash = None
    enrollment.webauthn_challenge = None
    _audit(customer, "credential_registered", "customer", customer.id, "Passkey verificada y vinculada.", enrollment=enrollment, credential=credential)
    _create_customer_session(customer, credential)
    db.session.commit()
    return customer, credential


def authentication_options(business):
    binding = secrets.token_urlsafe(32)
    rp_id, _origin = _webauthn_context()
    challenge = secrets.token_bytes(32)
    auth = CustomerAuthChallenge(
        business_id=business.id, browser_binding_hash=_sha(binding), challenge=challenge,
        expires_at=_now() + timedelta(seconds=current_app.config["CUSTOMER_AUTH_CHALLENGE_MAX_AGE"]),
    )
    db.session.add(auth)
    db.session.commit()
    session["customer_auth_challenge_id"] = auth.id
    session["customer_auth_binding"] = binding
    options = generate_authentication_options(
        rp_id=rp_id, challenge=challenge,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return json.loads(options_to_json(options))


def complete_authentication(business, credential_payload):
    challenge_id = session.get("customer_auth_challenge_id")
    binding = session.get("customer_auth_binding")
    challenge = CustomerAuthChallenge.query.filter_by(id=challenge_id, business_id=business.id).first()
    if not challenge or not binding or challenge.consumed_at or challenge.expires_at <= _now() or not hmac.compare_digest(challenge.browser_binding_hash, _sha(binding)):
        raise CustomerAuthError("La autenticación expiró o ya fue utilizada.", 409)
    claimed_at = _now()
    changed = CustomerAuthChallenge.query.filter_by(
        id=challenge.id, business_id=business.id, consumed_at=None,
    ).update({CustomerAuthChallenge.consumed_at: claimed_at}, synchronize_session=False)
    if changed != 1:
        db.session.rollback()
        raise CustomerAuthError("La autenticación ya está siendo procesada.", 409)
    db.session.commit()
    challenge = db.session.get(CustomerAuthChallenge, challenge.id)
    try:
        raw_id = base64url_to_bytes(credential_payload.get("id", ""))
    except Exception as exc:
        raise CustomerAuthError("La credencial no es válida.") from exc
    credential = CustomerCredential.query.filter_by(
        business_id=business.id, credential_id=raw_id, is_active=True,
    ).with_for_update().first()
    if not credential or credential.revoked_at:
        raise CustomerAuthError("No fue posible autenticar la credencial.", 401)
    rp_id, origin = _webauthn_context()
    try:
        verified = verify_authentication_response(
            credential=credential_payload, expected_challenge=challenge.challenge,
            expected_rp_id=rp_id, expected_origin=origin,
            credential_public_key=credential.public_key,
            credential_current_sign_count=credential.sign_count,
            require_user_verification=True,
        )
    except Exception as exc:
        CustomerAuthChallenge.query.filter_by(
            id=challenge.id, consumed_at=claimed_at,
        ).update({CustomerAuthChallenge.consumed_at: None}, synchronize_session=False)
        db.session.commit()
        raise CustomerAuthError("No fue posible autenticar la credencial.", 401) from exc
    credential.sign_count = verified.new_sign_count
    credential.last_used_at = _now()
    customer = Customer.query.filter_by(id=credential.customer_id, business_id=business.id).first()
    _create_customer_session(customer, credential)
    _audit(customer, "customer_authenticated", "customer", customer.id, "Autenticación WebAuthn válida.", credential=credential)
    db.session.commit()
    return customer


def current_customer():
    session_id = session.get("customer_session_id")
    verifier = session.get("customer_session_verifier")
    if not session_id or not verifier:
        return None
    auth_session = db.session.get(CustomerSession, session_id)
    if not auth_session or auth_session.revoked_at or auth_session.expires_at <= _now() or not hmac.compare_digest(auth_session.verifier_hash, _sha(verifier)):
        return None
    if _now() - auth_session.last_active_at > timedelta(seconds=current_app.config["CUSTOMER_SESSION_IDLE_SECONDS"]):
        return None
    credential = CustomerCredential.query.filter_by(
        id=auth_session.credential_id, business_id=auth_session.business_id,
        customer_id=auth_session.customer_id, is_active=True,
    ).first()
    customer = Customer.query.filter_by(
        id=auth_session.customer_id, business_id=auth_session.business_id,
    ).first()
    if not credential or credential.revoked_at or not customer or not customer.business.is_active:
        return None
    auth_session.last_active_at = _now()
    db.session.commit()
    return customer


def revoke_current_customer_session():
    session_id = session.get("customer_session_id")
    auth_session = db.session.get(CustomerSession, session_id) if session_id else None
    if auth_session and not auth_session.revoked_at:
        auth_session.revoked_at = _now()
        db.session.commit()
    session.clear()


def revoke_customer_access(*, staff, customer_id, reason):
    reason = reason.strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > 500 or not has_permission(staff, "authorize_customer_identity"):
        raise CustomerAuthError("Se requiere autorización y un motivo válido.", 403)
    customer = Customer.query.filter_by(id=customer_id, business_id=staff.business_id).first()
    if not customer:
        raise CustomerAuthError("Cliente no encontrado.", 404)
    now = _now()
    CustomerCredential.query.filter_by(business_id=staff.business_id, customer_id=customer.id, is_active=True).update({
        CustomerCredential.is_active: False, CustomerCredential.revoked_at: now,
        CustomerCredential.revoked_reason: reason,
    }, synchronize_session=False)
    CustomerSession.query.filter_by(business_id=staff.business_id, customer_id=customer.id, revoked_at=None).update({
        CustomerSession.revoked_at: now,
    }, synchronize_session=False)
    _audit(customer, "customer_access_revoked", "staff", staff.id, reason)
    db.session.commit()
