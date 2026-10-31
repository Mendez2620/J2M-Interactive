import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint, current_app, jsonify, redirect, render_template, request,
    send_file, session, url_for,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.extensions import db
from app.models import (
    Business, Customer, CustomerRecoveryAuthorization,
    CustomerRecoveryRateLimit,
)
from app.security import wants_json
from app.services import AppleWalletService, GoogleWalletService
from app.services.referral_participant_service import prepare_customer_wallet_invitation
from app.services.google_wallet import GoogleWalletAPIError, GoogleWalletConfigurationError


recovery_bp = Blueprint("recovery", __name__)
GENERIC_REQUEST_MESSAGE = "La recuperación pública está deshabilitada. Solicita asistencia autorizada directamente al negocio."


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
        challenge_pending=False,
        google_enabled=google_mode != "disabled",
        google_mock=google_mode == "mock",
        notice=GENERIC_REQUEST_MESSAGE if request.args.get("requested") else None,
    )


@recovery_bp.route("/recover/<business_slug>/request", methods=["POST"])
def request_recovery(business_slug):
    business = _business_or_404(business_slug)
    if wants_json():
        return jsonify({"status": "unavailable", "message": GENERIC_REQUEST_MESSAGE}), 410
    return render_template(
        "recover.html", business=business, customer=None,
        challenge_pending=False, google_mock=False,
        notice=GENERIC_REQUEST_MESSAGE,
    ), 410


@recovery_bp.route("/recover/<business_slug>/verify", methods=["POST"])
def verify_recovery(business_slug):
    business = _business_or_404(business_slug)
    return jsonify({"status": "error", "message": "La recuperación por código ya no está disponible."}), 410


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
        prepare_customer_wallet_invitation(customer)
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
        prepare_customer_wallet_invitation(verified[1])
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
