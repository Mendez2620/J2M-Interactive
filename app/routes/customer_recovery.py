import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, current_app, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.email_service import MailDeliveryError
from app.extensions import db
from app.models import (
    Business, Customer, CustomerRecoveryAuthorization,
    CustomerRecoveryChallenge, CustomerRecoveryRateLimit,
)
from app.security import valid_email, wants_json
from app.services import AppleWalletService, GoogleWalletService
from app.services.google_wallet import GoogleWalletAPIError, GoogleWalletConfigurationError


recovery_bp = Blueprint("recovery", __name__)
GENERIC_REQUEST_MESSAGE = "Si existe una tarjeta asociada a ese correo, recibirás instrucciones para recuperarla."


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hmac(value, purpose):
    key = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(key, f"{purpose}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _clear_recovery_session(revoke=False):
    authorization_id = session.pop("customer_recovery_authorization_id", None)
    session.pop("customer_recovery_authorization_token", None)
    session.pop("customer_recovery_challenge_id", None)
    if revoke and authorization_id:
        authorization = db.session.get(CustomerRecoveryAuthorization, authorization_id)
        if authorization and authorization.revoked_at is None:
            authorization.revoked_at = _utcnow()
            db.session.commit()


def _current_authorization(business):
    authorization_id = session.get("customer_recovery_authorization_id")
    token = session.get("customer_recovery_authorization_token")
    if not authorization_id or not token or not business.is_active:
        return None
    authorization = db.session.get(CustomerRecoveryAuthorization, authorization_id)
    now = _utcnow()
    if (
        not authorization or authorization.business_id != business.id
        or authorization.revoked_at is not None or authorization.expires_at <= now
        or not hmac.compare_digest(authorization.token_hash, _token_hash(token))
    ):
        _clear_recovery_session(revoke=bool(authorization))
        return None
    customer = db.session.get(Customer, authorization.customer_id)
    if not customer or customer.business_id != business.id:
        _clear_recovery_session(revoke=True)
        return None
    return authorization, customer


def _business_or_404(slug):
    return Business.query.filter_by(slug=slug).first_or_404()


def _generic_request_response(business):
    if wants_json():
        return jsonify({"status": "accepted", "message": GENERIC_REQUEST_MESSAGE}), 200
    return render_template(
        "recover.html", business=business, customer=None,
        challenge_pending=False, google_mock=False,
        notice=GENERIC_REQUEST_MESSAGE,
    ), 200


def _reserve_rate_limit(scope, business_scope, key_hash, limit, now, window_seconds):
    """Atomically claim one persistent quota slot, including under concurrency."""
    table = CustomerRecoveryRateLimit.__table__
    dialect = db.session.get_bind().dialect.name
    insert_factory = sqlite_insert if dialect == "sqlite" else postgresql_insert
    reserved_until = now + timedelta(seconds=window_seconds)
    for slot in range(limit):
        statement = insert_factory(table).values(
            scope=scope,
            business_scope=business_scope,
            key_hash=key_hash,
            slot=slot,
            reserved_until=reserved_until,
            updated_at=now,
        ).on_conflict_do_update(
            index_elements=["scope", "business_scope", "key_hash", "slot"],
            set_={"reserved_until": reserved_until, "updated_at": now},
            where=table.c.reserved_until <= now,
        )
        if db.session.execute(statement).rowcount == 1:
            return True
    return False


@recovery_bp.after_request
def protect_recovery_responses(response):
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@recovery_bp.route("/recover/<business_slug>", methods=["GET"])
def recover(business_slug):
    business = _business_or_404(business_slug)
    verified = _current_authorization(business)
    customer = verified[1] if verified else None
    google_mode = current_app.config.get("GOOGLE_WALLET_MODE", "mock") if customer else "disabled"
    return render_template(
        "recover.html", business=business, customer=customer,
        challenge_pending=bool(session.get("customer_recovery_challenge_id")),
        google_enabled=google_mode != "disabled",
        google_mock=google_mode == "mock",
        notice=GENERIC_REQUEST_MESSAGE if request.args.get("requested") else None,
    )


@recovery_bp.route("/recover/<business_slug>/request", methods=["POST"])
def request_recovery(business_slug):
    business = _business_or_404(business_slug)
    _clear_recovery_session(revoke=True)
    email = (request.get_json(silent=True) or request.form).get("email", "").strip().lower()
    now = _utcnow()
    retention_start = now - timedelta(seconds=current_app.config["RECOVERY_CHALLENGE_RETENTION"])
    CustomerRecoveryChallenge.query.filter(CustomerRecoveryChallenge.created_at < retention_start).delete(synchronize_session=False)
    email_hash = _hmac(email, "recovery-email")
    ip_hash = _hmac(request.remote_addr or "unknown", "recovery-ip")
    allowed = business.is_active and valid_email(email)
    if allowed:
        allowed = _reserve_rate_limit(
            "email", business.id, email_hash,
            current_app.config["RECOVERY_REQUEST_LIMIT_EMAIL"], now,
            current_app.config["RECOVERY_RATE_WINDOW"],
        ) and _reserve_rate_limit(
            "ip", 0, ip_hash,
            current_app.config["RECOVERY_REQUEST_LIMIT_IP"], now,
            current_app.config["RECOVERY_RATE_WINDOW"],
        )
        if allowed:
            db.session.commit()
        else:
            db.session.rollback()
    if not allowed:
        return _generic_request_response(business)
    customers = []
    if allowed:
        customers = Customer.query.filter(
            Customer.business_id == business.id,
            func.lower(Customer.email) == email,
        ).limit(2).all()

    code = f"{secrets.randbelow(100_000_000):08d}"
    challenge_id = secrets.token_hex(18)
    challenge = CustomerRecoveryChallenge(
        id=challenge_id,
        business_id=business.id,
        customer_id=customers[0].id if len(customers) == 1 else None,
        email_hash=email_hash,
        ip_hash=ip_hash,
        code_hash=_hmac(f"{challenge_id}:{code}", "recovery-code"),
        expires_at=now + timedelta(seconds=current_app.config["RECOVERY_CODE_MAX_AGE"]),
    )
    db.session.add(challenge)
    CustomerRecoveryChallenge.query.filter(
        CustomerRecoveryChallenge.business_id == business.id,
        CustomerRecoveryChallenge.email_hash == email_hash,
        CustomerRecoveryChallenge.id != challenge.id,
        CustomerRecoveryChallenge.consumed_at.is_(None),
    ).update({CustomerRecoveryChallenge.consumed_at: now}, synchronize_session=False)

    if allowed and len(customers) == 1:
        try:
            current_app.extensions["recovery_mailer"].send_recovery_code(email, code, business.name)
        except MailDeliveryError:
            db.session.rollback()
            CustomerRecoveryChallenge.query.filter(
                CustomerRecoveryChallenge.business_id == business.id,
                CustomerRecoveryChallenge.email_hash == email_hash,
                CustomerRecoveryChallenge.consumed_at.is_(None),
            ).update({CustomerRecoveryChallenge.consumed_at: now}, synchronize_session=False)
            challenge = CustomerRecoveryChallenge(
                id=secrets.token_hex(18), business_id=business.id, customer_id=None,
                email_hash=email_hash, ip_hash=ip_hash,
                code_hash=_hmac(secrets.token_urlsafe(32), "recovery-code"),
                expires_at=now + timedelta(seconds=current_app.config["RECOVERY_CODE_MAX_AGE"]),
            )
            db.session.add(challenge)

    db.session.commit()
    session["customer_recovery_challenge_id"] = challenge.id
    if wants_json():
        return jsonify({"status": "accepted", "message": GENERIC_REQUEST_MESSAGE}), 200
    return redirect(url_for("recovery.recover", business_slug=business.slug, requested="1"))


@recovery_bp.route("/recover/<business_slug>/verify", methods=["POST"])
def verify_recovery(business_slug):
    business = _business_or_404(business_slug)
    challenge_id = session.get("customer_recovery_challenge_id")
    code = str((request.get_json(silent=True) or request.form).get("code", "")).strip()
    challenge = db.session.get(CustomerRecoveryChallenge, challenge_id) if challenge_id else None
    now = _utcnow()
    valid_challenge = bool(
        challenge and challenge.business_id == business.id and challenge.customer_id
        and challenge.consumed_at is None and challenge.expires_at > now
        and challenge.attempts < current_app.config["RECOVERY_MAX_ATTEMPTS"]
    )
    supplied_hash = _hmac(f"{challenge.id}:{code}", "recovery-code") if challenge else _hmac(code, "recovery-code")
    code_matches = bool(valid_challenge and hmac.compare_digest(challenge.code_hash, supplied_hash))
    if not code_matches:
        if challenge and challenge.business_id == business.id and challenge.consumed_at is None and challenge.expires_at > now:
            CustomerRecoveryChallenge.query.filter(
                CustomerRecoveryChallenge.id == challenge.id,
                CustomerRecoveryChallenge.consumed_at.is_(None),
                CustomerRecoveryChallenge.attempts < current_app.config["RECOVERY_MAX_ATTEMPTS"],
            ).update({CustomerRecoveryChallenge.attempts: CustomerRecoveryChallenge.attempts + 1}, synchronize_session=False)
            db.session.commit()
        message = "El código no es válido o ya expiró."
        return (jsonify({"status": "error", "message": message}), 400) if wants_json() else (
            render_template("recover.html", business=business, customer=None, challenge_pending=True, google_mock=False, error=message), 400
        )

    consumed = CustomerRecoveryChallenge.query.filter(
        CustomerRecoveryChallenge.id == challenge.id,
        CustomerRecoveryChallenge.business_id == business.id,
        CustomerRecoveryChallenge.customer_id == challenge.customer_id,
        CustomerRecoveryChallenge.code_hash == supplied_hash,
        CustomerRecoveryChallenge.consumed_at.is_(None),
        CustomerRecoveryChallenge.expires_at > now,
        CustomerRecoveryChallenge.attempts < current_app.config["RECOVERY_MAX_ATTEMPTS"],
    ).update({CustomerRecoveryChallenge.consumed_at: now}, synchronize_session=False)
    if consumed != 1:
        db.session.rollback()
        return jsonify({"status": "error", "message": "El código no es válido o ya expiró."}), 409

    token = secrets.token_urlsafe(32)
    authorization = CustomerRecoveryAuthorization(
        business_id=business.id,
        customer_id=challenge.customer_id,
        token_hash=_token_hash(token),
        expires_at=now + timedelta(seconds=current_app.config["RECOVERY_SESSION_MAX_AGE"]),
    )
    db.session.add(authorization)
    db.session.commit()
    session.pop("customer_recovery_challenge_id", None)
    session["customer_recovery_authorization_id"] = authorization.id
    session["customer_recovery_authorization_token"] = token
    if wants_json():
        return jsonify({"status": "success", "redirect_url": url_for("recovery.recover", business_slug=business.slug)}), 200
    return redirect(url_for("recovery.recover", business_slug=business.slug))


@recovery_bp.route("/recover/<business_slug>/end", methods=["POST"])
def end_recovery(business_slug):
    _business_or_404(business_slug)
    _clear_recovery_session(revoke=True)
    return redirect(url_for("recovery.recover", business_slug=business_slug))


@recovery_bp.route("/recover/<business_slug>/wallet/apple", methods=["GET"])
def recovery_apple_wallet(business_slug):
    business = _business_or_404(business_slug)
    verified = _current_authorization(business)
    if not verified:
        return redirect(url_for("recovery.recover", business_slug=business_slug))
    customer = verified[1]
    try:
        return send_file(
            AppleWalletService().generate_pkpass(customer),
            mimetype="application/vnd.apple.pkpass", as_attachment=True,
            download_name=f"{business.slug}-pass.pkpass",
        )
    except Exception:
        current_app.logger.exception("Apple Wallet recovery issuance failed")
        return render_template("recover.html", business=business, customer=customer, challenge_pending=False, google_mock=False, error="No fue posible generar el pase de Apple Wallet."), 502


@recovery_bp.route("/recover/<business_slug>/wallet/google", methods=["GET"])
def recovery_google_wallet(business_slug):
    business = _business_or_404(business_slug)
    verified = _current_authorization(business)
    if not verified:
        return redirect(url_for("recovery.recover", business_slug=business_slug))
    try:
        _jwt_token, save_url = GoogleWalletService().create_jwt_pass(verified[1])
        return redirect(save_url)
    except GoogleWalletConfigurationError:
        current_app.logger.warning("google_wallet_recovery_unavailable business_id=%s", business.id)
        return render_template("recover.html", business=business, customer=verified[1], challenge_pending=False, google_enabled=False, google_mock=False, error="Google Wallet no está disponible temporalmente."), 503
    except GoogleWalletAPIError as exc:
        current_app.logger.warning("google_wallet_recovery_failed business_id=%s status_code=%s", business.id, exc.status_code)
        return render_template("recover.html", business=business, customer=verified[1], challenge_pending=False, google_enabled=True, google_mock=False, error="No fue posible preparar Google Wallet."), 502
    except Exception:
        current_app.logger.exception("Google Wallet recovery issuance failed")
        return render_template("recover.html", business=business, customer=verified[1], challenge_pending=False, google_mock=False, error="No fue posible generar el enlace de Google Wallet."), 502
