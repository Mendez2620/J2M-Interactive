import hashlib
import hmac
import secrets
from datetime import date, datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request, session, url_for
from sqlalchemy.exc import IntegrityError
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.extensions import db
from app.models import (Business, Customer, CustomerRegistrationIdentityClaim,
                        ReferralParticipant)
from app.phone_numbers import normalize_phone
from app.routes.customer_recovery import _reserve_rate_limit
from app.wallet_access import issue_wallet_token
from app.services.referral_participant_service import (
    create_pending_attribution, resolve_invitation,
)

business_bp = Blueprint("business", __name__)
_WALLET_DELIVERY_SESSION_KEY = "customer_wallet_delivery"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hmac(value, purpose):
    return hmac.new(current_app.config["SECRET_KEY"].encode(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def _identity_hmac(value):
    key = current_app.config["REGISTRATION_IDENTITY_HMAC_KEY"].encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def _start_wallet_delivery(customer):
    credential = secrets.token_urlsafe(32)
    session[_WALLET_DELIVERY_SESSION_KEY] = {
        "business_id": customer.business_id,
        "customer_id": customer.id,
        "credential": credential,
        "expires_at": int(datetime.now(timezone.utc).timestamp())
        + current_app.config["WALLET_DELIVERY_MAX_AGE"],
    }
    return credential


def _active_wallet_delivery(business, credential=None):
    delivery = session.get(_WALLET_DELIVERY_SESSION_KEY)
    now = int(datetime.now(timezone.utc).timestamp())
    if not isinstance(delivery, dict) or delivery.get("expires_at", 0) <= now:
        session.pop(_WALLET_DELIVERY_SESSION_KEY, None)
        return None
    expected = delivery.get("credential")
    if (
        delivery.get("business_id") != business.id
        or not isinstance(expected, str)
        or (credential is not None and not hmac.compare_digest(expected, credential))
    ):
        return None
    customer = db.session.get(Customer, delivery.get("customer_id"))
    if not customer or customer.business_id != business.id:
        session.pop(_WALLET_DELIVERY_SESSION_KEY, None)
        return None
    return delivery, customer


def _wallet_delivery_payload(business, customer, credential):
    token = issue_wallet_token(customer)
    google_url = None
    if current_app.config.get("GOOGLE_WALLET_MODE") != "disabled":
        google_url = f"/api/customers/{customer.id}/wallet/google?token={token}&format=redirect"
    return {
        "status": "success",
        "customer": {"id": customer.id, "full_name": customer.full_name},
        "google_wallet_url": google_url,
        "apple_wallet_url": f"/api/customers/{customer.id}/wallet/apple?token={token}",
        "wallet_delivery_url": url_for(
            "business.resume_wallet_delivery", slug=business.slug, credential=credential
        ),
    }


def _legal_document_hash(template_name, version):
    source, _, _ = current_app.jinja_loader.get_source(current_app.jinja_env, template_name)
    return hashlib.sha256(f"{version}\0{source}".encode()).hexdigest()


def _referral_serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="customer-referral-context-v1")


def _referral_context(participant):
    return _referral_serializer().dumps({
        "business_id": participant.business_id,
        "participant_id": participant.id,
        "participant_type": participant.participant_type,
    })


def _participant_from_context(value, business_id):
    if not isinstance(value, str) or not value:
        return None
    try:
        context = _referral_serializer().loads(
            value, max_age=current_app.config["REGISTRATION_CHALLENGE_RETENTION"]
        )
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(context, dict) or context.get("business_id") != business_id:
        return None
    participant_id = context.get("participant_id")
    participant_type = context.get("participant_type")
    if not isinstance(participant_id, int) or participant_type not in {
        "external_promoter", "staff", "customer_ambassador"
    }:
        return None
    participant = ReferralParticipant.query.filter_by(
        id=participant_id, business_id=business_id,
        participant_type=participant_type, is_enabled=True,
    ).first()
    return participant if participant and participant.currently_eligible() else None


def _payload():
    value = request.get_json(silent=True) if request.is_json else request.form.to_dict()
    return value if isinstance(value, dict) else None


def _truthy(value):
    return value is True or isinstance(value, str) and value.strip().lower() in {"1", "true", "on", "yes"}


def _validated(data):
    name, birth = data.get("full_name"), data.get("birthdate")
    if not isinstance(name, str) or not 2 <= len(name.strip()) <= 120:
        raise ValueError("Ingresa un nombre completo válido.")
    birthdate = None
    if birth not in (None, ""):
        if not isinstance(birth, str):
            raise ValueError("Ingresa una fecha de nacimiento válida.")
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
    return name.strip(), phone, birthdate, _truthy(data.get("marketing_consent"))


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
        referral_code = (request.args.get("ref") or "").strip()
        participant = resolve_invitation(business_id=business.id, code=referral_code)
        # A referral link is an explicit new-registration context. Never expose
        # a previous browser user's delivery capability on that page.
        active_delivery = None if referral_code else _active_wallet_delivery(business)
        wallet_delivery_url = None
        if active_delivery:
            delivery, _ = active_delivery
            wallet_delivery_url = url_for(
                "business.resume_wallet_delivery",
                slug=business.slug,
                credential=delivery["credential"],
            )
        return render_template(
            "join.html", business=business,
            referral_context=_referral_context(participant) if participant else "",
            terms_version=current_app.config["TERMS_VERSION"],
            privacy_version=current_app.config["PRIVACY_NOTICE_VERSION"],
            wallet_delivery_url=wallet_delivery_url,
            wallet_delivery_end_url=url_for("business.end_wallet_delivery", slug=business.slug),
        )
    data = _payload()
    if data is None:
        return jsonify({"status": "error", "message": "La solicitud no tiene un formato válido."}), 400
    try:
        name, phone, birthdate, marketing = _validated(data)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    now = _utcnow()
    phone_hash = _identity_hmac(phone)
    ip_hash = _hmac(request.remote_addr or "unknown", "registration-ip")
    phone_allowed = _reserve_rate_limit(
        "reg_phone", business.id, phone_hash,
        current_app.config["REGISTRATION_REQUEST_LIMIT_PHONE"], now,
        current_app.config["REGISTRATION_RATE_WINDOW"],
    )
    ip_allowed = _reserve_rate_limit(
        "reg_ip", 0, ip_hash,
        current_app.config["REGISTRATION_REQUEST_LIMIT_IP"], now,
        current_app.config["REGISTRATION_RATE_WINDOW"],
    )
    # Quota reservations must survive every later outcome, including duplicate
    # identities and integrity conflicts.
    db.session.commit()
    if not (phone_allowed and ip_allowed):
        db.session.rollback()
        return jsonify({"status": "error", "message": "Demasiadas solicitudes. Intenta nuevamente más tarde."}), 429

    participant = _participant_from_context(data.get("referral_context"), business.id)
    ua_hash = _hmac((request.user_agent.string or "unknown")[:512], "registration-user-agent")
    customer = Customer(
        business_id=business.id, full_name=name, email=None, phone=phone,
        birthdate=birthdate, email_verified_at=None, terms_accepted_at=now,
        terms_version=current_app.config["TERMS_VERSION"],
        terms_content_hash=_legal_document_hash("terms.html", current_app.config["TERMS_VERSION"]),
        privacy_accepted_at=now,
        privacy_version=current_app.config["PRIVACY_NOTICE_VERSION"],
        privacy_content_hash=_legal_document_hash("privacy.html", current_app.config["PRIVACY_NOTICE_VERSION"]),
        marketing_consent=marketing, marketing_consent_at=now if marketing else None,
        consent_ip_hash=ip_hash, consent_user_agent_hash=ua_hash,
    )
    try:
        active_business = db.session.query(Business).filter(
            Business.id == business.id, Business.is_active.is_(True)
        ).populate_existing().with_for_update().one_or_none()
        if active_business is None:
            db.session.rollback()
            return jsonify({"status": "error", "message": "El programa no está disponible en este momento."}), 403
        if Customer.query.filter_by(business_id=business.id, phone=phone).first():
            db.session.rollback()
            return jsonify({"status": "error", "message": "No fue posible completar el registro. Solicita asistencia al negocio."}), 409
        db.session.add(customer)
        db.session.flush()
        db.session.add(CustomerRegistrationIdentityClaim(
            business_id=business.id, identity_type="phone",
            identity_hash=phone_hash, customer_id=customer.id,
        ))
        if participant and active_business.referrals_enabled:
            locked_participant = db.session.query(ReferralParticipant).filter(
                ReferralParticipant.id == participant.id,
                ReferralParticipant.business_id == business.id,
                ReferralParticipant.is_enabled.is_(True),
            ).with_for_update().one_or_none()
            if locked_participant and locked_participant.currently_eligible():
                referrer_phone = (
                    locked_participant.promoter.phone if locked_participant.promoter
                    else locked_participant.staff.phone if locked_participant.staff
                    else locked_participant.customer.phone if locked_participant.customer
                    else None
                )
                # A verifiable same-customer ambassador is always rejected.
                if not (
                    locked_participant.participant_type == "customer_ambassador"
                    and locked_participant.customer_id == customer.id
                ) and referrer_phone != phone:
                    create_pending_attribution(
                        business=active_business, customer=customer,
                        participant=locked_participant,
                    )
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"status": "error", "message": "No fue posible completar el registro. Solicita asistencia al negocio."}), 409

    credential = _start_wallet_delivery(customer)
    payload = _wallet_delivery_payload(business, customer, credential)
    payload["message"] = "Tarjeta creada."
    return jsonify(payload), 201


@business_bp.get("/join/<slug>/wallet-delivery")
def resume_wallet_delivery(slug):
    business, error = _business(slug)
    if error:
        return error
    if not business.is_active:
        return jsonify({"status": "error", "message": "El programa no está disponible en este momento."}), 403
    credential = request.args.get("credential", "")
    active_delivery = _active_wallet_delivery(business, credential)
    if not active_delivery:
        return jsonify({"status": "error", "message": "El acceso temporal a la tarjeta venció o no es válido."}), 403
    _, customer = active_delivery
    response = jsonify(_wallet_delivery_payload(business, customer, credential))
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@business_bp.post("/join/<slug>/wallet-delivery/end")
def end_wallet_delivery(slug):
    business, error = _business(slug)
    if error:
        return error
    delivery = session.get(_WALLET_DELIVERY_SESSION_KEY)
    if isinstance(delivery, dict) and delivery.get("business_id") == business.id:
        session.pop(_WALLET_DELIVERY_SESSION_KEY, None)
    return jsonify({"status": "success"})



@business_bp.route("/join/<slug>/verify", methods=["POST"])
def verify_registration(slug):
    business, error = _business(slug)
    if error:
        return error
    return jsonify({
        "status": "error",
        "message": "La verificación por correo ya no se utiliza. Vuelve al registro para crear tu tarjeta.",
    }), 410
