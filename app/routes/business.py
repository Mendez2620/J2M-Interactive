from datetime import datetime
from flask import Blueprint, jsonify, render_template, request, url_for
from app.extensions import db
from app.models import Business, Customer
from app.services import GoogleWalletService

business_bp = Blueprint("business", __name__)


@business_bp.route("/api/business/ping", methods=["GET"])
def ping():
    return jsonify({"message": "Business module ready"}), 200


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

    if request.method == "GET":
        return render_template("join.html", business=business)

    # Handle POST
    data = request.get_json(silent=True) or request.form.to_dict()
    full_name = (data.get("full_name") or "").strip()
    email = (data.get("email") or "").strip() or None
    phone = (data.get("phone") or "").strip() or None
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
    if email:
        customer = Customer.query.filter_by(business_id=business.id, email=email).first()
    if not customer and phone:
        customer = Customer.query.filter_by(business_id=business.id, phone=phone).first()

    if customer:
        # Update details if necessary
        customer.full_name = full_name
        if birthdate:
            customer.birthdate = birthdate
        if phone and not customer.phone:
            customer.phone = phone
        if email and not customer.email:
            customer.email = email
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
    google_service = GoogleWalletService()
    _, google_save_url = google_service.create_jwt_pass(customer)
    apple_wallet_url = f"/api/customers/{customer.id}/wallet/apple"

    return jsonify({
        "status": "success",
        "message": "Registro completado con éxito.",
        "customer": customer.to_dict(),
        "google_wallet_url": google_save_url,
        "apple_wallet_url": apple_wallet_url,
    }), 201
