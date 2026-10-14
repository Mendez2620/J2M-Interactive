import math
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import jwt
from flask import Blueprint, current_app, g, jsonify, render_template, request
from sqlalchemy import or_

from app.extensions import db
from app.models import Business, Customer, Staff, StaffSession, Transaction
from app.staff_auth import decode_token, issue_token_pair, revoke_staff_sessions, staff_any_channel_required, staff_required

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
        auth_session = db.session.get(StaffSession, payload["sid"])
        staff = db.session.get(Staff, int(payload["sub"]))
        from app.staff_auth import _hash_jti
        valid = auth_session and not auth_session.is_revoked and _hash_jti(payload["jti"]) == auth_session.refresh_jti_hash
    except (jwt.PyJWTError, TypeError, ValueError):
        valid, auth_session, staff = False, None, None
    if auth_session and auth_session.is_revoked and staff:
        revoke_staff_sessions(staff)
        db.session.commit()
    if not valid or not staff or payload["ver"] != staff.auth_version:
        return _error("Refresh token inválido o reutilizado.", 401)
    channel = payload["chn"]
    if channel not in {"web", "mobile"}:
        return _error("Canal inválido.", 401)
    if channel != auth_session.channel or not staff.is_active or not staff.business.is_active or not getattr(staff, f"{channel}_access_enabled") or not getattr(staff.business, f"{channel}_access_enabled"):
        return _error("Acceso inactivo.", 403)
    auth_session.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
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
    existing = Transaction.query.filter_by(request_id=request_id).first()
    if existing:
        if existing.business_id != g.staff.business_id:
            return _error("Identificador de solicitud no disponible.", 409)
        return jsonify({"status": "success", "idempotent_replay": True, "transaction": existing.to_dict()}), 200
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
        if action.startswith("earn"):
            last_tx = Transaction.query.filter_by(business_id=business.id, customer_id=customer.id, type="earn").order_by(Transaction.timestamp.desc()).first()
            if last_tx:
                delta = (datetime.now(timezone.utc).replace(tzinfo=None) - last_tx.timestamp.replace(tzinfo=None)).total_seconds() / 60
                if delta < business.cooldown_minutes:
                    return _error("Operación bloqueada por cooldown.", 429)
        if action == "earn_stamp":
            customer.current_stamps += 1
            tx = Transaction(business_id=business.id, customer_id=customer.id, staff_id=g.staff.id,
                             type="earn", stamps_amount=1, purchase_amount=purchase, request_id=request_id)
        elif action == "redeem_stamp":
            needed = business.stamps_reward_limit
            if customer.current_stamps < needed:
                return _error("Sellos insuficientes.", 400)
            customer.current_stamps -= needed
            tx = Transaction(business_id=business.id, customer_id=customer.id, staff_id=g.staff.id,
                             type="redeem", stamps_amount=needed, purchase_amount=purchase, request_id=request_id)
        elif action == "earn_points":
            purchase = _number(data.get("purchase_amount"), "purchase_amount", positive=True)
            points = purchase * business.points_per_currency
            if not math.isfinite(points) or points <= 0:
                raise ValueError("Regla de puntos inválida.")
            customer.current_points += points
            tx = Transaction(business_id=business.id, customer_id=customer.id, staff_id=g.staff.id,
                             type="earn", points_amount=points, purchase_amount=purchase, request_id=request_id)
        else:
            points = _number(data.get("amount"), "amount", positive=True)
            if customer.current_points < points:
                return _error("Puntos insuficientes.", 400)
            customer.current_points -= points
            tx = Transaction(business_id=business.id, customer_id=customer.id, staff_id=g.staff.id,
                             type="redeem", points_amount=points, purchase_amount=purchase, request_id=request_id)
    except ValueError as exc:
        db.session.rollback()
        return _error(str(exc), 400)
    db.session.add(tx)
    db.session.commit()
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
        from app.services.reward_notification_service import RewardNotificationService
        RewardNotificationService().send_reward_ready(customer, business)
        reward_notified = True
    return jsonify({"status": "success", "customer": {"id": customer.id,
        "current_points": customer.current_points, "current_stamps": customer.current_stamps},
        "transaction": tx.to_dict(), "wallet_sync": {
            "google_status": google_sync_result.get("status") if isinstance(google_sync_result, dict) else "skipped",
            "apple_devices_notified": apple_notified_count}, "reward_notified": reward_notified}), 200
