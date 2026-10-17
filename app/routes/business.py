from datetime import datetime
from flask import Blueprint, jsonify, render_template, request
from app.extensions import db
from app.models import Business, Customer
from app.services import GoogleWalletService
from app.wallet_access import issue_wallet_token
from app.phone_numbers import normalize_phone

business_bp = Blueprint("business", __name__)


@business_bp.route("/api/business/ping", methods=["GET"])
def ping():
    return jsonify({"message": "Business module ready"}), 200


@business_bp.route("/privacy", methods=["GET"])
def privacy_policy():
    """Renders the Privacy Policy page."""
    return render_template("privacy.html")


@business_bp.route("/terms", methods=["GET"])
def terms_and_conditions():
    """Renders the Terms and Conditions page."""
    return render_template("terms.html")


@business_bp.route("/join/<slug>", methods=["GET", "POST"])
def join_business(slug: str):
    """
    Public registration page and API for customer onboarding.
    
    GET: Renders the branded customer registration form.
    POST: Creates/retrieves customer and returns Google & Apple Wallet URLs.
    """
    business = Business.query.filter_by(slug=slug).first()
    if not business:
        if request.is_json or request.headers.get("Accept") == "application/json":
            return jsonify({"status": "error", "message": f"Negocio '{slug}' no encontrado"}), 404
        return render_template(
            "base.html",
            content="<div class='p-8 text-center text-rose-600 font-bold'>Negocio no encontrado</div>"
        ), 404

    # Check if business subscription is active
    if not business.is_active:
        if request.method == "POST":
            return jsonify({
                "status": "error",
                "message": "El servicio para este negocio se encuentra temporalmente suspendido por suscripción.",
            }), 403
        return render_template("join.html", business=business)

    if request.method == "GET":
        return render_template("join.html", business=business)

    # Handle POST
    data = request.get_json(silent=True) or request.form.to_dict()
    full_name = (data.get("full_name") or "").strip()
    email = (data.get("email") or "").strip() or None
    customer = Customer.query.filter_by(business_id=business.id, email=email).first() if email else None
    if customer:
        return jsonify({
            "status": "error",
            "message": "Ya existe una cuenta con esos datos. Utiliza el proceso de recuperación verificada.",
        }), 409
    try:
        phone = normalize_phone(data.get("phone"), data.get("phone_country") or "+52")
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    birthdate_str = data.get("birthdate")

    if not full_name:
        return jsonify({"status": "error", "message": "El nombre completo es obligatorio."}), 400

    birthdate = None
    if birthdate_str:
        try:
            birthdate = datetime.strptime(birthdate_str, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"status": "error", "message": "Formato de fecha de nacimiento inválido (esperado YYYY-MM-DD)."}), 400

    # Look up if customer already exists for this business by email or phone
    customer = None
    if phone:
        customer = Customer.query.filter_by(business_id=business.id, phone=phone).first()

    if customer:
        return jsonify({
            "status": "error",
            "message": "Ya existe una cuenta con esos datos. Utiliza el proceso de recuperación verificada.",
        }), 409
    else:
        customer = Customer(
            business_id=business.id,
            full_name=full_name,
            email=email,
            phone=phone,
            birthdate=birthdate,
            current_points=0.0,
            current_stamps=0,
        )
        db.session.add(customer)

    db.session.commit()

    # Generate Wallet links
    wallet_token = issue_wallet_token(customer)
    google_wallet_url = f"/api/customers/{customer.id}/wallet/google?token={wallet_token}"
    apple_wallet_url = f"/api/customers/{customer.id}/wallet/apple?token={wallet_token}"

    return jsonify({
        "status": "success",
        "message": "Registro completado con éxito.",
        "customer": {"id": customer.id, "full_name": customer.full_name,
                     "birthdate": customer.birthdate.isoformat() if customer.birthdate else None},
        "google_wallet_url": google_wallet_url,
        "apple_wallet_url": apple_wallet_url,
    }), 201
