from datetime import date
from flask import Blueprint, jsonify, render_template, request
from app.extensions import db
from app.models import Business, Customer, Staff, Transaction

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def calculate_age_distribution():
    """Calculates customer demographics into standard age buckets based on birthdate."""
    today = date.today()
    customers = Customer.query.all()
    
    buckets = {
        "<18": 0,
        "18-24": 0,
        "25-34": 0,
        "35-49": 0,
        "50+": 0,
        "desconocido": 0,
    }

    for c in customers:
        if not c.birthdate:
            buckets["desconocido"] += 1
            continue
        try:
            age = today.year - c.birthdate.year - (
                (today.month, today.day) < (c.birthdate.month, c.birthdate.day)
            )
            if age < 18:
                buckets["<18"] += 1
            elif 18 <= age <= 24:
                buckets["18-24"] += 1
            elif 25 <= age <= 34:
                buckets["25-34"] += 1
            elif 35 <= age <= 49:
                buckets["35-49"] += 1
            else:
                buckets["50+"] += 1
        except Exception:
            buckets["desconocido"] += 1

    return buckets


@admin_bp.route("", methods=["GET"])
@admin_bp.route("/dashboard", methods=["GET"])
def dashboard():
    """Super Admin Dashboard view with cross-tenant analytics."""
    businesses = Business.query.all()
    total_businesses = len(businesses)
    total_customers = Customer.query.count()
    total_transactions = Transaction.query.count()
    
    # Calculate sum of purchase amounts
    total_volume_query = db.session.query(db.func.sum(Transaction.purchase_amount)).scalar()
    total_purchase_volume = float(total_volume_query or 0.0)

    age_stats = calculate_age_distribution()
    staff_list = Staff.query.all()

    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({
            "status": "success",
            "metrics": {
                "total_businesses": total_businesses,
                "total_customers": total_customers,
                "total_transactions": total_transactions,
                "total_purchase_volume": total_purchase_volume,
                "age_distribution": age_stats,
            },
            "businesses": [b.to_dict() for b in businesses],
            "staff": [s.to_dict() for s in staff_list],
        }), 200

    return render_template(
        "admin/dashboard.html",
        total_businesses=total_businesses,
        total_customers=total_customers,
        total_transactions=total_transactions,
        total_purchase_volume=total_purchase_volume,
        age_stats=age_stats,
        businesses=businesses,
        staff_list=staff_list,
    )


@admin_bp.route("/business/<int:business_id>", methods=["GET"])
def get_business_details(business_id: int):
    """Detailed analytics and customer list for a specific business."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    customers = [c.to_dict() for c in business.customers.all()]
    staff_members = [s.to_dict() for s in business.staff_members.all()]
    transactions_count = business.transactions.count()
    total_vol = db.session.query(db.func.sum(Transaction.purchase_amount)).filter_by(business_id=business.id).scalar()

    return jsonify({
        "status": "success",
        "business": business.to_dict(),
        "stats": {
            "total_customers": len(customers),
            "total_staff": len(staff_members),
            "total_transactions": transactions_count,
            "total_purchase_volume": float(total_vol or 0.0),
        },
        "customers": customers,
        "staff": staff_members,
    }), 200


@admin_bp.route("/business/<int:business_id>/staff", methods=["POST"])
def create_staff(business_id: int):
    """Creates a new Staff member (Manager, Cashier, Admin) for a given business."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    data = request.get_json() or request.form.to_dict()
    username = (data.get("username") or "").strip()
    password = data.get("password")
    role = (data.get("role") or "cashier").lower().strip()
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip() or None
    phone = (data.get("phone") or "").strip() or None

    if not username or not password:
        return jsonify({"status": "error", "message": "Usuario y contraseña son requeridos."}), 400

    if role not in ("admin", "manager", "cashier"):
        return jsonify({"status": "error", "message": "Rol inválido. Debe ser 'admin', 'manager' o 'cashier'."}), 400

    # Check if username exists for this business
    existing_staff = Staff.query.filter_by(business_id=business_id, username=username).first()
    if existing_staff:
        return jsonify({"status": "error", "message": f"El usuario '{username}' ya existe para este negocio."}), 409

    staff_member = Staff(
        business_id=business.id,
        name=name,
        username=username,
        email=email,
        phone=phone,
        role=role,
        is_active=True,
    )
    staff_member.set_password(password)

    db.session.add(staff_member)
    db.session.commit()

    return jsonify({
        "status": "success",
        "message": f"Usuario de staff '{username}' ({role}) creado exitosamente.",
        "staff": staff_member.to_dict(),
    }), 201


@admin_bp.route("/staff", methods=["GET"])
def list_all_staff():
    """Lists all staff members across businesses."""
    staff_members = Staff.query.all()
    return jsonify({
        "status": "success",
        "staff": [s.to_dict() for s in staff_members],
    }), 200
