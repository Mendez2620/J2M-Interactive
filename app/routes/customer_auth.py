from functools import wraps

from flask import Blueprint, g, jsonify, redirect, render_template, request, url_for

from app.extensions import db
from app.manager_auth import manager_portal_required, staff_portal_required
from app.models import Business
from app.services.customer_auth_service import (
    CustomerAuthError, approve_linking_enrollment, authentication_options,
    claim_linking_enrollment, complete_authentication, complete_registration,
    create_linking_enrollment, current_customer, registration_options,
    revoke_current_customer_session, revoke_customer_access,
)
from app.services.auth_rate_limit_service import (
    AuthRateLimitUnavailable, clear_auth_attempts, reserve_auth_attempt,
)
from app.services.referral_participant_service import (
    ReferralParticipantError, configure_customer_ambassador,
)


customer_auth_bp = Blueprint("customer_auth", __name__, url_prefix="/customer-auth")


def _error(exc):
    return jsonify({"status": "error", "message": str(exc)}), exc.status_code


def customer_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        customer = current_customer()
        if not customer:
            revoke_current_customer_session()
            return redirect(url_for("customer_auth.customer_login", business_slug=kwargs.get("business_slug", "")))
        g.customer_identity = customer
        return view(*args, **kwargs)
    return wrapped


def _create(staff):
    data = (request.get_json(silent=True) if request.is_json else request.form) or {}
    try:
        customer_id = int(data.get("customer_id"))
        if customer_id <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return _error(CustomerAuthError("El identificador del cliente no es válido."))
    try:
        enrollment, secret = create_linking_enrollment(
            staff=staff, customer_id=customer_id,
            human_check_note=data.get("human_check_note"),
            recovery_mode=str(data.get("recovery_mode", "")).lower() in {"1", "true", "yes"},
        )
    except CustomerAuthError as exc:
        return _error(exc)
    return jsonify({
        "status": "success", "enrollment_id": enrollment.id,
        "expires_at": enrollment.expires_at.isoformat(),
        "enrollment_path": url_for("customer_auth.enrollment_page"),
        "linking_secret": secret,
    }), 201


def _approve(staff, enrollment_id):
    data = (request.get_json(silent=True) if request.is_json else request.form) or {}
    try:
        enrollment = approve_linking_enrollment(
            staff=staff, enrollment_id=enrollment_id, pairing_code=data.get("pairing_code"),
        )
    except CustomerAuthError as exc:
        return _error(exc)
    return jsonify({"status": "success", "enrollment_id": enrollment.id}), 200


def _revoke(staff, customer_id):
    data = (request.get_json(silent=True) if request.is_json else request.form) or {}
    try:
        revoke_customer_access(staff=staff, customer_id=customer_id, reason=data.get("reason"))
    except CustomerAuthError as exc:
        return _error(exc)
    return jsonify({"status": "success"}), 200


@customer_auth_bp.get("/staff/linking")
@staff_portal_required("authorize_customer_identity")
def staff_linking_page():
    return render_template(
        "customer_auth/staff_linking.html", staff=g.staff_browser,
        create_endpoint=url_for("customer_auth.staff_create_linking"),
        approve_endpoint_template="/customer-auth/staff/linking/{id}/approve",
    )


@customer_auth_bp.post("/staff/linking")
@staff_portal_required("authorize_customer_identity")
def staff_create_linking():
    return _create(g.staff_browser)


@customer_auth_bp.post("/staff/linking/<enrollment_id>/approve")
@staff_portal_required("authorize_customer_identity")
def staff_approve_linking(enrollment_id):
    return _approve(g.staff_browser, enrollment_id)


@customer_auth_bp.post("/staff/customers/<int:customer_id>/revoke")
@staff_portal_required("authorize_customer_identity")
def staff_revoke_customer(customer_id):
    return _revoke(g.staff_browser, customer_id)


@customer_auth_bp.get("/manager/linking")
@manager_portal_required("authorize_customer_identity")
def manager_linking_page():
    return render_template(
        "customer_auth/staff_linking.html", staff=g.manager,
        create_endpoint=url_for("customer_auth.manager_create_linking"),
        approve_endpoint_template="/customer-auth/manager/linking/{id}/approve",
    )


@customer_auth_bp.post("/manager/linking")
@manager_portal_required("authorize_customer_identity")
def manager_create_linking():
    return _create(g.manager)


@customer_auth_bp.post("/manager/linking/<enrollment_id>/approve")
@manager_portal_required("authorize_customer_identity")
def manager_approve_linking(enrollment_id):
    return _approve(g.manager, enrollment_id)


@customer_auth_bp.post("/manager/customers/<int:customer_id>/revoke")
@manager_portal_required("authorize_customer_identity")
def manager_revoke_customer(customer_id):
    return _revoke(g.manager, customer_id)


@customer_auth_bp.get("/enroll")
def enrollment_page():
    return render_template("customer_auth/enroll.html")


@customer_auth_bp.post("/enroll/claim")
def enrollment_claim():
    secret = request.form.get("linking_secret") or ""
    try:
        reservation = reserve_auth_attempt("customer-linking", secret[:64], request.remote_addr)
    except AuthRateLimitUnavailable:
        return render_template("customer_auth/error.html", message="La vinculación no está disponible temporalmente."), 503
    if reservation is None:
        return render_template("customer_auth/error.html", message="Demasiados intentos. Intenta más tarde."), 429
    try:
        enrollment, pairing_code = claim_linking_enrollment(secret)
    except CustomerAuthError as exc:
        return render_template("customer_auth/error.html", message=str(exc)), exc.status_code
    clear_auth_attempts("customer-linking", reservation)
    return render_template(
        "customer_auth/pairing.html", pairing_code=pairing_code,
        enrollment_id=enrollment.id,
    )


@customer_auth_bp.post("/enroll/options")
def enrollment_options():
    try:
        return jsonify(registration_options())
    except CustomerAuthError as exc:
        return _error(exc)


@customer_auth_bp.post("/enroll/complete")
def enrollment_complete():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return _error(CustomerAuthError("La respuesta WebAuthn no es válida."))
    try:
        customer, _credential = complete_registration(payload)
    except CustomerAuthError as exc:
        return _error(exc)
    return jsonify({
        "status": "success",
        "redirect": url_for("customer_auth.customer_security", business_slug=customer.business.slug),
    })


@customer_auth_bp.get("/login/<business_slug>")
def customer_login(business_slug):
    business = Business.query.filter_by(slug=business_slug, is_active=True).first_or_404()
    return render_template("customer_auth/login.html", business=business)


@customer_auth_bp.post("/login/<business_slug>/options")
def customer_login_options(business_slug):
    business = Business.query.filter_by(slug=business_slug, is_active=True).first_or_404()
    try:
        return jsonify(authentication_options(business))
    except CustomerAuthError as exc:
        return _error(exc)


@customer_auth_bp.post("/login/<business_slug>/complete")
def customer_login_complete(business_slug):
    business = Business.query.filter_by(slug=business_slug, is_active=True).first_or_404()
    payload = request.get_json(silent=True)
    identity_hint = payload.get("id", "") if isinstance(payload, dict) else "invalid-payload"
    try:
        reservation = reserve_auth_attempt(
            "customer-passkey", f"{business.id}:{str(identity_hint)[:128]}", request.remote_addr,
        )
    except AuthRateLimitUnavailable:
        return jsonify({"status": "error", "message": "Autenticación temporalmente no disponible."}), 503
    if reservation is None:
        return jsonify({"status": "error", "message": "Demasiados intentos. Intenta más tarde."}), 429
    try:
        complete_authentication(business, payload)
    except CustomerAuthError as exc:
        return _error(exc)
    clear_auth_attempts("customer-passkey", reservation)
    return jsonify({
        "status": "success",
        "redirect": url_for("customer_auth.customer_security", business_slug=business.slug),
    })


@customer_auth_bp.get("/portal/<business_slug>/security")
@customer_required
def customer_security(business_slug):
    if g.customer_identity.business.slug != business_slug:
        return redirect(url_for("customer_auth.customer_security", business_slug=g.customer_identity.business.slug))
    return render_template("customer_auth/security.html", customer=g.customer_identity)


@customer_auth_bp.post("/portal/<business_slug>/ambassador-consent")
@customer_required
def customer_ambassador_consent(business_slug):
    customer = g.customer_identity
    if customer.business.slug != business_slug:
        return jsonify({"status": "error", "message": "Operación no disponible."}), 404
    data = (request.get_json(silent=True) if request.is_json else request.form) or {}
    if not hasattr(data, "get"):
        return _error(CustomerAuthError("La solicitud no es válida."))
    enabled = data.get("enabled") is True or str(data.get("enabled", "")).lower() in {"1", "true", "yes"}
    if enabled:
        return jsonify({
            "status": "error",
            "message": "La participación de clientes embajadores estará disponible cuando se publique el consentimiento versionado.",
        }), 409
    try:
        participant, replayed, historical = configure_customer_ambassador(
            customer=customer, enabled=enabled,
            consent_version=None, consent_evidence=None,
            actor_type="customer", actor_id=customer.id,
            reason=data.get("reason"), request_id=request.headers.get("Idempotency-Key"),
            authorization_verified=True,
        )
    except ReferralParticipantError as exc:
        return jsonify({"status": "error", "message": str(exc)}), exc.status_code
    return jsonify({
        "status": "success", "enabled": historical["is_enabled"],
        "idempotent_replay": replayed,
        # Deliberately omit invitation_code until 8C.3B2.
        "participant_type": participant.participant_type,
    })


@customer_auth_bp.post("/logout")
def customer_logout():
    revoke_current_customer_session()
    return redirect(url_for("main.health_check"))
