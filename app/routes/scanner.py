import hashlib
import json
import math
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from decimal import Decimal

import jwt
from flask import Blueprint, current_app, g, jsonify, render_template, request
from sqlalchemy import or_
from app.extensions import db
from app.models import Business, Customer, Staff, StaffSession, Transaction
from app.services.loyalty_transaction_service import (
    LoyaltyTransactionError, normalized_points, process_loyalty_transaction,
)
from app.staff_auth import _hash_jti, decode_token, issue_token_pair, revoke_staff_sessions, staff_any_channel_required, staff_required

scanner_bp = Blueprint("scanner", __name__)
_login_attempts = defaultdict(deque)


def _error(message, code):
    return jsonify({"status": "error", "message": message}), code


def _number(value, name, *, positive=False, integer=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} inválido.")
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{name} inválido.")
    if integer and not number.is_integer():
        raise ValueError(f"{name} debe ser entero.")
    if abs(number) > current_app.config["MAX_TRANSACTION_AMOUNT"]:
        raise ValueError(f"{name} excede el límite permitido.")
    return int(number) if integer else number


def _operation_fingerprint(customer_id, action, purchase_amount, points_amount, stamps_amount):
    canonical = {
        "action": action,
        "customer_id": customer_id,
        "points_amount": format(points_amount, ".12g"),
        "purchase_amount": format(purchase_amount, ".12g"),
        "stamps_amount": stamps_amount,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _replay_or_conflict(existing, fingerprint):
    if existing.request_fingerprint and existing.request_fingerprint == fingerprint:
        return jsonify({"status": "success", "idempotent_replay": True,
                        "transaction": existing.to_dict()}), 200
    return _error("Idempotency-Key ya fue utilizado para otra operación.", 409)


@scanner_bp.route("/scanner", methods=["GET"])
def scanner_view():
    if current_app.config.get("ENV") == "production" or not current_app.debug:
        return _error("La interfaz web de escaneo no está disponible.", 404)
    return render_template("scanner.html", businesses=[])


@scanner_bp.route("/api/scanner/ping", methods=["GET"])
def ping():
    return jsonify({"message": "Scanner module ready"}), 200


@scanner_bp.route("/api/scanner/login", methods=["POST"])
def staff_login():
    data = request.get_json(silent=True) or {}
    identity = (data.get("username") or data.get("email") or "").strip()
    password = data.get("password") or ""
    business_slug = (data.get("business_slug") or "").strip()
    channel = (data.get("channel") or "mobile").strip().lower()
    if channel not in {"web", "mobile"}:
        return _error("Canal inválido.", 400)
    key = f"{request.remote_addr}:{identity.lower()}"
    now = time.monotonic()
    attempts = _login_attempts[key]
    window = current_app.config["AUTH_RATE_LIMIT_WINDOW"]
    while attempts and attempts[0] < now - window:
        attempts.popleft()
    if len(attempts) >= current_app.config["AUTH_RATE_LIMIT_ATTEMPTS"]:
        return _error("Demasiados intentos. Intenta más tarde.", 429)
    if not identity or not password:
        return _error("Usuario y contraseña son obligatorios.", 400)
    query = Staff.query.join(Business).filter(or_(Staff.username == identity, Staff.email == identity))
    if business_slug:
        query = query.filter(Business.slug == business_slug)
    candidates = query.all()
    if len(candidates) > 1 and not business_slug:
        attempts.append(now)
        return _error("business_slug es obligatorio para identificar el negocio.", 400)
    staff = next((candidate for candidate in candidates if candidate.check_password(password)), None)
    if not staff:
        attempts.append(now)
        return _error("Credenciales inválidas.", 401)
    if not staff.is_active or not staff.business or not staff.business.is_active:
        return _error("Cuenta o negocio inactivo.", 403)
    if not getattr(staff.business, f"{channel}_access_enabled") or not getattr(staff, f"{channel}_access_enabled"):
        return _error(f"Acceso {channel} deshabilitado.", 403)
    attempts.clear()
    access_token, refresh_token = issue_token_pair(staff, channel)
    return jsonify({"status": "success", "access_token": access_token, "refresh_token": refresh_token,
        "token_type": "Bearer", "expires_in": current_app.config["JWT_ACCESS_TOKEN_EXPIRES"],
        "user": {"id": staff.id, "name": staff.name, "username": staff.username, "role": staff.role,
                 "business_id": staff.business_id, "business_name": staff.business.name}}), 200


@scanner_bp.route("/api/scanner/refresh", methods=["POST"])
def refresh_token():
    raw = (request.get_json(silent=True) or {}).get("refresh_token", "")
    try:
        payload = decode_token(raw, "refresh")
        staff = db.session.get(Staff, int(payload["sub"]))
    except (jwt.PyJWTError, TypeError, ValueError):
        return _error("Refresh token inválido o reutilizado.", 401)
    if not staff or payload["ver"] != staff.auth_version:
        return _error("Refresh token inválido o reutilizado.", 401)
    channel = payload["chn"]
    if channel not in {"web", "mobile"}:
        return _error("Canal inválido.", 401)
    if not staff.is_active or not staff.business.is_active or not getattr(staff, f"{channel}_access_enabled") or not getattr(staff.business, f"{channel}_access_enabled"):
        return _error("Acceso inactivo.", 403)
    consumed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    consumed = StaffSession.query.filter(
        StaffSession.id == payload["sid"],
        StaffSession.staff_id == staff.id,
        StaffSession.channel == channel,
        StaffSession.refresh_jti_hash == _hash_jti(payload["jti"]),
        StaffSession.revoked_at.is_(None),
        StaffSession.expires_at > consumed_at,
    ).update({StaffSession.revoked_at: consumed_at}, synchronize_session=False)
    if consumed != 1:
        db.session.rollback()
        revoke_staff_sessions(staff)
        db.session.commit()
        return _error("Refresh token inválido o reutilizado.", 401)
    db.session.commit()
    access, refresh = issue_token_pair(staff, channel)
    return jsonify({"status": "success", "access_token": access, "refresh_token": refresh,
                    "token_type": "Bearer", "expires_in": current_app.config["JWT_ACCESS_TOKEN_EXPIRES"]}), 200


@scanner_bp.route("/api/scanner/logout", methods=["POST"])
@staff_any_channel_required
def logout():
    revoke_staff_sessions(g.staff)
    db.session.commit()
    return jsonify({"status": "success"}), 200


@scanner_bp.route("/api/scanner/lookup", methods=["POST"])
@staff_required("mobile", "scanner_operate")
def lookup_customer():
    token = ((request.get_json(silent=True) or {}).get("token") or "").strip()
    if not token:
        return _error("Token de escaneo obligatorio.", 400)
    customer = Customer.query.filter_by(business_id=g.staff.business_id, qr_code_token=token).first()
    if not customer:
        return _error("Cliente no encontrado.", 404)
    return jsonify({"status": "success", "customer": {"id": customer.id, "full_name": customer.full_name,
        "current_points": customer.current_points, "current_stamps": customer.current_stamps},
        "business": {"id": customer.business.id, "name": customer.business.name,
        "loyalty_type": customer.business.loyalty_type, "stamps_reward_limit": customer.business.stamps_reward_limit}}), 200


@scanner_bp.route("/api/scanner/process", methods=["POST"])
@staff_required("mobile", "scanner_operate")
def process_transaction():
    data = request.get_json(silent=True) or {}
    request_id = (request.headers.get("Idempotency-Key") or data.get("request_id") or "").strip()
    token = (data.get("token") or "").strip()
    action = data.get("action")
    if not request_id or len(request_id) > 64:
        return _error("Idempotency-Key obligatorio e inválido.", 400)
    customer = Customer.query.filter_by(business_id=g.staff.business_id, qr_code_token=token).first()
    if not customer:
        return _error("Cliente no encontrado.", 404)
    business = customer.business
    allowed = {"stamps": {"earn_stamp", "redeem_stamp"}, "points": {"earn_points", "redeem_points"}}
    if action not in allowed.get(business.loyalty_type, set()):
        return _error("Acción incompatible con el programa de lealtad.", 400)
    try:
        purchase = _number(data.get("purchase_amount", 0), "purchase_amount")
        if purchase < 0:
            raise ValueError("purchase_amount inválido.")
        if business.loyalty_type == "stamps" and data.get("amount") is not None:
            supplied = _number(data.get("amount"), "amount", positive=True, integer=True)
            expected = 1 if action == "earn_stamp" else business.stamps_reward_limit
            if supplied != expected:
                raise ValueError("La cantidad de sellos la determina el servidor.")
        if action == "earn_points" and data.get("amount") not in (None, 0, 0.0, "0", "0.0"):
            raise ValueError("Los puntos se calculan a partir de purchase_amount.")
        if action == "earn_stamp":
            tx_type, points_amount, stamps_amount = "earn", 0.0, 1
        elif action == "redeem_stamp":
            tx_type, points_amount, stamps_amount = "redeem", 0.0, business.stamps_reward_limit
        elif action == "earn_points":
            purchase = _number(data.get("purchase_amount"), "purchase_amount", positive=True)
            points_amount = normalized_points(
                Decimal(str(purchase)) * Decimal(str(business.points_per_currency))
            )
            if not math.isfinite(points_amount) or points_amount <= 0:
                raise ValueError("Regla de puntos inválida.")
            tx_type, stamps_amount = "earn", 0
        else:
            points_amount = normalized_points(_number(data.get("amount"), "amount", positive=True))
            if points_amount <= 0:
                raise ValueError("amount inválido.")
            tx_type, stamps_amount = "redeem", 0
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc), 400)

    fingerprint = _operation_fingerprint(
        customer.id, action, purchase, points_amount, stamps_amount
    )
    try:
        result = process_loyalty_transaction(
            business=business, customer_id=customer.id, staff_id=g.staff.id,
            action=action, tx_type=tx_type, points_amount=points_amount,
            stamps_amount=stamps_amount, purchase_amount=purchase,
            request_id=request_id, fingerprint=fingerprint,
        )
    except LoyaltyTransactionError as exc:
        return _error(exc.message, exc.status_code)
    if result.replay:
        return _replay_or_conflict(result.transaction, fingerprint)
    tx, customer = result.transaction, result.customer
    google_sync_result, apple_notified_count = None, 0
    try:
        from app.services import GoogleWalletService
        from app.routes.apple_webservice import notify_apple_wallet_devices
        google_sync_result = GoogleWalletService().update_loyalty_object(customer)
        apple_notified_count = notify_apple_wallet_devices(customer)
    except Exception:
        current_app.logger.exception("Wallet sync failed after transaction %s", tx.id)
    reward_notified = False
    if action.startswith("earn") and business.loyalty_type == "stamps" and customer.current_stamps >= business.stamps_reward_limit:
        try:
            from app.services.reward_notification_service import RewardNotificationService
            RewardNotificationService().send_reward_ready(customer, business)
            reward_notified = True
        except Exception:
            current_app.logger.exception("Reward notification failed after transaction %s", tx.id)
    return jsonify({"status": "success", "customer": {"id": customer.id,
        "current_points": customer.current_points, "current_stamps": customer.current_stamps},
        "transaction": tx.to_dict(), "wallet_sync": {
            "google_status": google_sync_result.get("status") if isinstance(google_sync_result, dict) else "skipped",
            "apple_devices_notified": apple_notified_count}, "reward_notified": reward_notified}), 200
