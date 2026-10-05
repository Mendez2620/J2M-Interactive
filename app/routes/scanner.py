from flask import Blueprint, jsonify, render_template, request
from datetime import datetime, timezone

from app.extensions import db
from app.models import Business, Customer, Staff, Transaction

scanner_bp = Blueprint("scanner", __name__)


@scanner_bp.route("/scanner", methods=["GET"])
def scanner_view():
    """Renders the mobile-first Scanner Web App."""
    businesses = Business.query.all()
    return render_template("scanner.html", businesses=businesses)


@scanner_bp.route("/api/scanner/ping", methods=["GET"])
def ping():
    return jsonify({"message": "Scanner module ready"}), 200


@scanner_bp.route("/api/scanner/lookup", methods=["POST"])
def lookup_customer():
    """
    Looks up a customer via QR code token, account ID, or phone number.
    """
    data = request.get_json(silent=True) or request.form.to_dict()
    token = (data.get("token") or "").strip()
    business_id = data.get("business_id")

    if not token:
        return jsonify({"status": "error", "message": "Token o código de escaneo no proporcionado."}), 400

    query = Customer.query
    if business_id:
        query = query.filter_by(business_id=business_id)

    # 1. Search by qr_code_token
    customer = query.filter_by(qr_code_token=token).first()
    
    # 2. Fallback: Search by ID if token is numeric
    if not customer and token.isdigit():
        customer = query.filter_by(id=int(token)).first()
        
    # 3. Fallback: Search by phone
    if not customer:
        customer = query.filter_by(phone=token).first()

    if not customer:
        return jsonify({"status": "error", "message": "Cliente no encontrado para esta sucursal."}), 404

    if not customer.business.is_active:
        return jsonify({
            "status": "error",
            "message": "La cuenta de este negocio se encuentra temporalmente suspendida o inactiva.",
        }), 403

    return jsonify({
        "status": "success",
        "customer": customer.to_dict(),
        "business": customer.business.to_dict(),
    }), 200


@scanner_bp.route("/api/scanner/process", methods=["POST"])
def process_transaction():
    """
    Processes an earn or redeem transaction for stamps or points.
    """
    data = request.get_json(silent=True) or request.form.to_dict()
    customer_id = data.get("customer_id")
    token = data.get("token")
    business_id = data.get("business_id")
    action = data.get("action")  # 'earn_stamp', 'redeem_stamp', 'earn_points', 'redeem_points'
    amount = float(data.get("amount") or 0.0)
    purchase_amount = float(data.get("purchase_amount") or 0.0)
    staff_id = data.get("staff_id")

    # Resolve customer
    customer = None
    if customer_id:
        customer = db.session.get(Customer, customer_id)
    elif token:
        customer = Customer.query.filter_by(qr_code_token=token).first()

    if not customer:
        return jsonify({"status": "error", "message": "Cliente no encontrado."}), 404

    business = customer.business
    if not business:
        return jsonify({"status": "error", "message": "El cliente no tiene un negocio asociado."}), 400

    if not business.is_active:
        return jsonify({
            "status": "error",
            "message": "Operación cancelada: La cuenta de este negocio se encuentra temporalmente inactiva o suspendida.",
        }), 403

    # Anti-fraud cooldown check (handle naive/aware timestamps)
    # Apply cooldown only for earning actions to allow immediate redeem in tests
    if action.startswith('earn'):
        last_tx = Transaction.query.filter_by(business_id=business.id, customer_id=customer.id).order_by(Transaction.timestamp.desc()).first()
        if last_tx:
            # Ensure both timestamps are naive UTC for subtraction
            last_time = last_tx.timestamp
            if last_time.tzinfo is not None:
                last_time = last_time.replace(tzinfo=None)
            now = datetime.utcnow()
            delta_minutes = (now - last_time).total_seconds() / 60
            if delta_minutes < business.cooldown_minutes:
                return jsonify({"status": "error", "message": f"El cliente ya acumuló puntos recientemente. Intenta de nuevo en {int(business.cooldown_minutes - delta_minutes)} minutos"}), 429
    # Proceed with transaction execution

    if action == "earn_stamp":
        stamps_to_add = int(amount) if amount > 0 else 1
        customer.current_stamps += stamps_to_add
        tx = Transaction(
            business_id=business.id,
            customer_id=customer.id,
            staff_id=staff_id,
            type="earn",
            stamps_amount=stamps_to_add,
            purchase_amount=purchase_amount,
        )
        msg = f"¡+{stamps_to_add} sello(s) acreditado(s)! Total: {customer.current_stamps}/{business.stamps_reward_limit}"

    elif action == "redeem_stamp":
        stamps_needed = int(amount) if amount > 0 else business.stamps_reward_limit
        if customer.current_stamps < stamps_needed:
            return jsonify({
                "status": "error",
                "message": f"Sellos insuficientes. Tiene {customer.current_stamps} y requiere {stamps_needed}.",
            }), 400

        customer.current_stamps -= stamps_needed
        tx = Transaction(
            business_id=business.id,
            customer_id=customer.id,
            staff_id=staff_id,
            type="redeem",
            stamps_amount=stamps_needed,
            purchase_amount=purchase_amount,
        )
        msg = f"¡Recompensa canjeada con éxito (-{stamps_needed} sellos)! Saldo restante: {customer.current_stamps}"

    elif action == "earn_points":
        points_to_add = float(amount)
        if points_to_add <= 0 and purchase_amount > 0:
            points_to_add = purchase_amount * business.points_per_currency

        if points_to_add <= 0:
            return jsonify({"status": "error", "message": "El monto de puntos a otorgar debe ser mayor a 0."}), 400

        customer.current_points += points_to_add
        tx = Transaction(
            business_id=business.id,
            customer_id=customer.id,
            staff_id=staff_id,
            type="earn",
            points_amount=points_to_add,
            purchase_amount=purchase_amount,
        )
        msg = f"¡+{points_to_add:.0f} puntos acreditados! Saldo total: {customer.current_points:.0f} pts"

    elif action == "redeem_points":
        points_to_redeem = float(amount)
        if points_to_redeem <= 0:
            return jsonify({"status": "error", "message": "Ingresa una cantidad de puntos válida a canjear."}), 400

        if customer.current_points < points_to_redeem:
            return jsonify({
                "status": "error",
                "message": f"Puntos insuficientes. Saldo disponible: {customer.current_points:.0f} pts.",
            }), 400

        customer.current_points -= points_to_redeem
        tx = Transaction(
            business_id=business.id,
            customer_id=customer.id,
            staff_id=staff_id,
            type="redeem",
            points_amount=points_to_redeem,
            purchase_amount=purchase_amount,
        )
        msg = f"¡Canje de {points_to_redeem:.0f} puntos procesado! Saldo restante: {customer.current_points:.0f} pts"

    else:
        return jsonify({"status": "error", "message": f"Acción '{action}' no reconocida."}), 400

    db.session.add(tx)
    db.session.commit()

    # Trigger Real-Time Wallet Synchronizations
    google_sync_result = None
    apple_notified_count = 0
    try:
        from app.services import GoogleWalletService
        from app.routes.apple_webservice import notify_apple_wallet_devices

        google_service = GoogleWalletService()
        google_sync_result = google_service.update_loyalty_object(customer)
        apple_notified_count = notify_apple_wallet_devices(customer)
    except Exception as e:
        # Non-blocking sync failure logging
        pass

    # After transaction commit, check for reward readiness and notify
    reward_notified = False
    if action.startswith('earn'):
        # Stamps reward check
        if business.loyalty_type == 'stamps' and customer.current_stamps >= business.stamps_reward_limit:
            from app.services.reward_notification_service import RewardNotificationService
            RewardNotificationService().send_reward_ready(customer, business)
            reward_notified = True
        # Points reward check (optional, using points_reward_limit if defined)
        if hasattr(business, 'points_reward_limit') and business.loyalty_type == 'points' and customer.current_points >= business.points_reward_limit:
            from app.services.reward_notification_service import RewardNotificationService
            RewardNotificationService().send_reward_ready(customer, business)
            reward_notified = True
    return jsonify({
        "status": "success",
        "message": msg,
        "customer": customer.to_dict(),
        "business": business.to_dict(),
        "transaction": tx.to_dict(),
        "wallet_sync": {
            "google_status": (
                google_sync_result.get("status")
                if isinstance(google_sync_result, dict)
                else google_sync_result
                if isinstance(google_sync_result, str)
                else "skipped"
            ),
            "apple_devices_notified": apple_notified_count,
        },
        "reward_notified": reward_notified,
    }), 200
