import hashlib
import hmac
import secrets
import time
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, render_template, request, session
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (Business, Customer, CustomerRegistrationChallenge,
                        CustomerRegistrationIdentityClaim, RegistrationEmailJob)
from app.phone_numbers import normalize_phone
from app.routes.customer_recovery import _reserve_rate_limit
from app.security import valid_email
from app.services.registration_email_service import encrypt_registration_code
from app.wallet_access import issue_wallet_token

business_bp = Blueprint("business", __name__)
GENERIC_REGISTRATION_MESSAGE = "Si los datos pueden registrarse, recibirás un código para confirmar tu correo."


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hmac(value, purpose):
    return hmac.new(current_app.config["SECRET_KEY"].encode(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def _identity_hmac(value):
    key = current_app.config["REGISTRATION_IDENTITY_HMAC_KEY"].encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def _legal_document_hash(template_name, version):
    source, _, _ = current_app.jinja_loader.get_source(current_app.jinja_env, template_name)
    return hashlib.sha256(f"{version}\0{source}".encode()).hexdigest()


def _generic_response(started_at):
    minimum = max(0, current_app.config["REGISTRATION_RESPONSE_MIN_MS"]) / 1000
    remaining = minimum - (time.monotonic() - started_at)
    if remaining > 0:
        time.sleep(remaining)
    return jsonify({"status": "accepted", "message": GENERIC_REGISTRATION_MESSAGE}), 202


def _payload():
    value = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    return value if isinstance(value, dict) else None


def _truthy(value):
    return value is True or isinstance(value, str) and value.strip().lower() in {"1", "true", "on", "yes"}


def _validated(data):
    name, email, birth = data.get("full_name"), data.get("email"), data.get("birthdate")
    if not isinstance(name, str) or not 2 <= len(name.strip()) <= 120:
        raise ValueError("Ingresa un nombre completo válido.")
    if not isinstance(email, str) or not valid_email(email.strip().lower()):
        raise ValueError("Ingresa un correo electrónico válido.")
    if not isinstance(birth, str):
        raise ValueError("La fecha de nacimiento es obligatoria.")
    try:
        birthdate = datetime.strptime(birth.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("Ingresa una fecha de nacimiento válida.") from None
    if birthdate > date.today() or birthdate.year < 1900:
        raise ValueError("Ingresa una fecha de nacimiento válida.")
    try:
        phone = normalize_phone(data.get("phone"), data.get("phone_country") or "+52")
    except (TypeError, ValueError) as exc:
        raise ValueError(str(exc)) from None
    if not phone:
        raise ValueError("El teléfono móvil es obligatorio.")
    if not _truthy(data.get("terms_accepted")) or not _truthy(data.get("privacy_accepted")):
        raise ValueError("Debes aceptar los Términos y el Aviso de Privacidad.")
    return name.strip(), email.strip().lower(), phone, birthdate, _truthy(data.get("marketing_consent"))


def _business(slug):
    business = Business.query.filter_by(slug=slug).first()
    if business:
        return business, None
    if request.is_json or request.headers.get("Accept") == "application/json":
        return None, (jsonify({"status": "error", "message": "Negocio no encontrado."}), 404)
    return None, (render_template("base.html", content="<div class='p-8 text-center'>Negocio no encontrado</div>"), 404)


@business_bp.after_request
def no_store_join(response):
    if request.path.startswith("/join/"):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


@business_bp.route("/api/business/ping")
def ping():
    return jsonify({"message": "Business module ready"})


@business_bp.route("/privacy")
def privacy_policy():
    return render_template("privacy.html", privacy_version=current_app.config["PRIVACY_NOTICE_VERSION"])


@business_bp.route("/terms")
def terms_and_conditions():
    return render_template("terms.html", terms_version=current_app.config["TERMS_VERSION"])


@business_bp.route("/join/<slug>", methods=["GET", "POST"])
def join_business(slug):
    business, error = _business(slug)
    if error:
        return error
    if not business.is_active:
        if request.method == "POST":
            return jsonify({"status": "error", "message": "El servicio para este negocio se encuentra temporalmente suspendido."}), 403
        return render_template("join.html", business=business)
    if request.method == "GET":
        return render_template(
            "join.html", business=business,
            terms_version=current_app.config["TERMS_VERSION"],
            privacy_version=current_app.config["PRIVACY_NOTICE_VERSION"],
        )
    started_at = time.monotonic()
    data = _payload()
    if data is None:
        return jsonify({"status": "error", "message": "La solicitud no tiene un formato válido."}), 400
    try:
        name, email, phone, birthdate, marketing = _validated(data)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    now = _utcnow()
    email_hash, phone_hash = _identity_hmac(email), _identity_hmac(phone)
    ip_hash = _hmac(request.remote_addr or "unknown", "registration-ip")
    ua_hash = _hmac((request.user_agent.string or "unknown")[:512], "registration-user-agent")
    cutoff = now - timedelta(seconds=current_app.config["REGISTRATION_CHALLENGE_RETENTION"])
    CustomerRegistrationChallenge.query.filter(CustomerRegistrationChallenge.created_at < cutoff).delete(synchronize_session=False)
    allowed = _reserve_rate_limit("reg_email", business.id, email_hash, current_app.config["REGISTRATION_REQUEST_LIMIT_EMAIL"], now, current_app.config["REGISTRATION_RATE_WINDOW"])
    allowed = allowed and _reserve_rate_limit("reg_ip", 0, ip_hash, current_app.config["REGISTRATION_REQUEST_LIMIT_IP"], now, current_app.config["REGISTRATION_RATE_WINDOW"])
    if allowed:
        db.session.commit()
    else:
        db.session.rollback()
        session.pop("customer_registration_challenge_id", None)
        return _generic_response(started_at)
    existing_email = Customer.query.filter(
        Customer.business_id == business.id, func.lower(Customer.email) == email
    ).first()
    existing_phone = Customer.query.filter(
        Customer.business_id == business.id, Customer.phone == phone
    ).first()
    if existing_email or existing_phone:
        # Never send registration mail for an existing identity. Historical
        # records may not have a verified email, and a newly supplied address
        # must not learn that its accompanying phone belongs to another user.
        session.pop("customer_registration_challenge_id", None)
        return _generic_response(started_at)
    code = f"{secrets.randbelow(100_000_000):08d}"
    challenge = CustomerRegistrationChallenge(
        business_id=business.id, full_name=name, email=email, email_hash=email_hash,
        phone=phone, phone_hash=phone_hash, birthdate=birthdate, code_hash="pending",
        terms_version=current_app.config["TERMS_VERSION"], privacy_version=current_app.config["PRIVACY_NOTICE_VERSION"],
        terms_accepted_at=now, privacy_accepted_at=now,
        terms_content_hash=_legal_document_hash("terms.html", current_app.config["TERMS_VERSION"]),
        privacy_content_hash=_legal_document_hash("privacy.html", current_app.config["PRIVACY_NOTICE_VERSION"]),
        marketing_consent=marketing, consent_ip_hash=ip_hash, consent_user_agent_hash=ua_hash,
        marketing_consent_at=now if marketing else None,
        expires_at=now + timedelta(seconds=current_app.config["REGISTRATION_CODE_MAX_AGE"]),
    )
    db.session.add(challenge)
    db.session.flush()
    challenge.code_hash = _hmac(f"{challenge.id}:{code}", "registration-code")
    CustomerRegistrationChallenge.query.filter(
        CustomerRegistrationChallenge.business_id == business.id,
        CustomerRegistrationChallenge.email_hash == email_hash,
        CustomerRegistrationChallenge.id != challenge.id,
        CustomerRegistrationChallenge.consumed_at.is_(None),
    ).update({CustomerRegistrationChallenge.consumed_at: now}, synchronize_session=False)
    db.session.add(RegistrationEmailJob(
        business_id=business.id,
        challenge_id=challenge.id,
        encrypted_code=encrypt_registration_code(code),
    ))
    db.session.commit()
    session["customer_registration_challenge_id"] = challenge.id
    return _generic_response(started_at)


@business_bp.route("/join/<slug>/verify", methods=["POST"])
def verify_registration(slug):
    business, error = _business(slug)
    if error:
        return error
    data = _payload()
    if data is None or not isinstance(data.get("code"), (str, int)):
        return jsonify({"status": "error", "message": "El código no es válido o ya expiró."}), 400
    challenge_id, now = session.get("customer_registration_challenge_id"), _utcnow()
    challenge = db.session.get(CustomerRegistrationChallenge, challenge_id) if challenge_id else None
    valid = bool(
        challenge and challenge.business_id == business.id and challenge.consumed_at is None
        and challenge.expires_at > now and challenge.attempts < current_app.config["REGISTRATION_MAX_ATTEMPTS"]
        and challenge.terms_accepted_at and challenge.privacy_accepted_at
        and challenge.terms_content_hash and challenge.privacy_content_hash
    )
    supplied = _hmac(f"{challenge.id}:{str(data['code']).strip()}", "registration-code") if challenge else _hmac(str(data["code"]), "registration-code")
    if not valid or not hmac.compare_digest(challenge.code_hash, supplied):
        if valid:
            CustomerRegistrationChallenge.query.filter(CustomerRegistrationChallenge.id == challenge.id, CustomerRegistrationChallenge.consumed_at.is_(None), CustomerRegistrationChallenge.attempts < current_app.config["REGISTRATION_MAX_ATTEMPTS"]).update({CustomerRegistrationChallenge.attempts: CustomerRegistrationChallenge.attempts + 1}, synchronize_session=False)
            db.session.commit()
        return jsonify({"status": "error", "message": "El código no es válido o ya expiró."}), 400
    active_business = db.session.query(Business).filter(
        Business.id == business.id, Business.is_active.is_(True)
    ).with_for_update().one_or_none()
    if active_business is None:
        CustomerRegistrationChallenge.query.filter(
            CustomerRegistrationChallenge.id == challenge.id,
            CustomerRegistrationChallenge.consumed_at.is_(None),
        ).update({CustomerRegistrationChallenge.consumed_at: now}, synchronize_session=False)
        db.session.commit()
        session.pop("customer_registration_challenge_id", None)
        return jsonify({"status": "error", "message": "El programa no está disponible en este momento."}), 403
    consumed = CustomerRegistrationChallenge.query.filter(
        CustomerRegistrationChallenge.id == challenge.id, CustomerRegistrationChallenge.business_id == business.id,
        CustomerRegistrationChallenge.consumed_at.is_(None), CustomerRegistrationChallenge.expires_at > now,
        CustomerRegistrationChallenge.attempts < current_app.config["REGISTRATION_MAX_ATTEMPTS"],
    ).update({CustomerRegistrationChallenge.consumed_at: now}, synchronize_session=False)
    if consumed != 1:
        db.session.rollback()
        return jsonify({"status": "error", "message": "El código no es válido o ya fue utilizado."}), 409
    duplicate = Customer.query.filter(Customer.business_id == business.id, or_(func.lower(Customer.email) == challenge.email, Customer.phone == challenge.phone)).first()
    if duplicate:
        db.session.commit()
        session.pop("customer_registration_challenge_id", None)
        return jsonify({"status": "error", "message": "No fue posible completar el registro. Utiliza la recuperación verificada."}), 409
    customer = Customer(
        business_id=business.id, full_name=challenge.full_name, email=challenge.email, phone=challenge.phone,
        birthdate=challenge.birthdate, email_verified_at=now, terms_accepted_at=challenge.terms_accepted_at,
        terms_version=challenge.terms_version, terms_content_hash=challenge.terms_content_hash,
        privacy_accepted_at=challenge.privacy_accepted_at, privacy_version=challenge.privacy_version,
        privacy_content_hash=challenge.privacy_content_hash,
        marketing_consent=challenge.marketing_consent, marketing_consent_at=challenge.marketing_consent_at,
        consent_ip_hash=challenge.consent_ip_hash, consent_user_agent_hash=challenge.consent_user_agent_hash,
    )
    try:
        db.session.add(customer)
        db.session.flush()
        db.session.add_all([
            CustomerRegistrationIdentityClaim(business_id=business.id, identity_type="email", identity_hash=challenge.email_hash, customer_id=customer.id),
            CustomerRegistrationIdentityClaim(business_id=business.id, identity_type="phone", identity_hash=challenge.phone_hash, customer_id=customer.id),
        ])
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        CustomerRegistrationChallenge.query.filter_by(id=challenge.id).update({CustomerRegistrationChallenge.consumed_at: now})
        db.session.commit()
        session.pop("customer_registration_challenge_id", None)
        return jsonify({"status": "error", "message": "No fue posible completar el registro. Utiliza la recuperación verificada."}), 409
    session.pop("customer_registration_challenge_id", None)
    token = issue_wallet_token(customer)
    google_url = f"/api/customers/{customer.id}/wallet/google?token={token}&format=redirect" if current_app.config.get("GOOGLE_WALLET_MODE") != "disabled" else None
    return jsonify({"status": "success", "message": "Registro verificado y tarjeta creada.", "customer": {"id": customer.id, "full_name": customer.full_name}, "google_wallet_url": google_url, "apple_wallet_url": f"/api/customers/{customer.id}/wallet/apple?token={token}"}), 201
