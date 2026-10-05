from datetime import date, datetime
from flask import Blueprint, jsonify, render_template, request, send_file, redirect, url_for
from app.extensions import db
from app.models import Business, BusinessCategory, Customer, Staff, Transaction
from app.services import ExportService

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
    """Super Admin Dashboard view with cross-tenant analytics and business listings."""
    businesses = Business.query.order_by(Business.created_at.desc()).all()
    total_businesses = len(businesses)
    total_customers = Customer.query.count()
    total_transactions = Transaction.query.count()
    
    total_volume_query = db.session.query(db.func.sum(Transaction.purchase_amount)).scalar()
    total_purchase_volume = float(total_volume_query or 0.0)

    age_stats = calculate_age_distribution()
    staff_list = Staff.query.order_by(Staff.created_at.desc()).all()
    categories = BusinessCategory.query.order_by(BusinessCategory.name).all()

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
            "categories": [c.to_dict() for c in categories],
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
        categories=categories,
    )


@admin_bp.route("/business/create", methods=["POST"])
def create_business():
    """CRUD Endpoint to create a new Business / Tenant."""
    data = request.get_json(silent=True) or request.form.to_dict()
    name = (data.get("name") or "").strip()
    slug = (data.get("slug") or "").strip().lower()

    if not name or not slug:
        return jsonify({"status": "error", "message": "Nombre y slug son obligatorios."}), 400

    # Ensure slug uniqueness
    existing = Business.query.filter_by(slug=slug).first()
    if existing:
        return jsonify({"status": "error", "message": f"El slug '{slug}' ya está en uso."}), 409

    category_id = data.get("category_id")
    if category_id:
        try:
            category_id = int(category_id)
        except (ValueError, TypeError):
            category_id = None

    loyalty_type = data.get("loyalty_type") or "stamps"
    points_per_currency = float(data.get("points_per_currency") or 1.0)
    stamps_reward_limit = int(data.get("stamps_reward_limit") or 10)
    primary_color = data.get("primary_color") or "#1E3A8A"
    secondary_color = data.get("secondary_color") or "#FFFFFF"
    logo_url = data.get("logo_url") or None
    latitude = float(data.get("latitude")) if data.get("latitude") is not None and str(data.get("latitude")).strip() != "" else None
    longitude = float(data.get("longitude")) if data.get("longitude") is not None and str(data.get("longitude")).strip() != "" else None
    is_active = True if str(data.get("is_active", "true")).lower() in ("true", "1", "t") else False

    business = Business(
        name=name,
        slug=slug,
        category_id=category_id,
        logo_url=logo_url,
        primary_color=primary_color,
        secondary_color=secondary_color,
        loyalty_type=loyalty_type,
        points_per_currency=points_per_currency,
        stamps_reward_limit=stamps_reward_limit,
        latitude=latitude,
        longitude=longitude,
        is_active=is_active,
    )
    db.session.add(business)
    db.session.commit()

    return jsonify({
        "status": "success",
        "message": f"Negocio '{business.name}' creado exitosamente.",
        "business": business.to_dict(),
    }), 201


@admin_bp.route("/business/<int:business_id>", methods=["GET"])
def get_business_details(business_id: int):
    """Detailed analytics, audit log, customer list, and staff for a specific business."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    customers = business.customers.order_by(Customer.created_at.desc()).all()
    transactions = business.transactions.order_by(Transaction.timestamp.desc()).all()
    staff_members = business.staff_members.all()
    total_volume = sum(t.purchase_amount for t in transactions)

    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({
            "status": "success",
            "business": business.to_dict(),
            "stats": {
                "total_customers": len(customers),
                "total_staff": len(staff_members),
                "total_transactions": len(transactions),
                "total_purchase_volume": float(total_volume),
            },
            "customers": [c.to_dict() for c in customers],
            "transactions": [t.to_dict() for t in transactions],
            "staff": [s.to_dict() for s in staff_members],
        }), 200

    return render_template(
        "admin/business_detail.html",
        business=business,
        customers=customers,
        transactions=transactions,
        staff_members=staff_members,
        total_volume=total_volume,
    )


@admin_bp.route("/business/<int:business_id>/toggle-status", methods=["POST"])
def toggle_business_status(business_id: int):
    """Toggles business is_active status (activate/suspend)."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    business.is_active = not business.is_active
    db.session.commit()

    status_str = "activado" if business.is_active else "suspendido"
    if request.is_json or request.headers.get("Accept") == "application/json":
        return jsonify({
            "status": "success",
            "message": f"Negocio '{business.name}' {status_str} exitosamente.",
            "is_active": business.is_active,
        }), 200

    return redirect(f"/admin/business/{business.id}")


@admin_bp.route("/business/<int:business_id>/export/excel", methods=["GET"])
def export_business_excel(business_id: int):
    """Exports business customer list and transaction audit log to Excel workbook (.xlsx)."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    excel_stream = ExportService.generate_excel_report(business)
    filename = f"{business.slug}_reporte_clientes.xlsx"

    return send_file(
        excel_stream,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=filename,
    )


@admin_bp.route("/business/<int:business_id>/export/pdf", methods=["GET"])
def export_business_pdf(business_id: int):
    """Exports business CRM metrics and recent tables to a PDF report."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    pdf_stream = ExportService.generate_pdf_report(business)
    filename = f"{business.slug}_reporte_gerencial.pdf"

    return send_file(
        pdf_stream,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
    )


@admin_bp.route("/business/<int:business_id>/staff", methods=["POST"])
def create_staff(business_id: int):
    """Creates a new Staff member (Manager, Cashier, Admin) for a given business."""
    business = db.session.get(Business, business_id)
    if not business:
        return jsonify({"status": "error", "message": "Negocio no encontrado"}), 404

    data = request.get_json(silent=True) or request.form.to_dict()
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
    staff_members = Staff.query.order_by(Staff.created_at.desc()).all()
    return jsonify({
        "status": "success",
        "staff": [s.to_dict() for s in staff_members],
    }), 200


@admin_bp.route("/categories", methods=["GET", "POST"])
def manage_categories():
    """Lists or creates business categories."""
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        name = (data.get("name") or "").strip()
        slug = (data.get("slug") or "").strip().lower() or name.lower().replace(" ", "-")

        if not name:
            return jsonify({"status": "error", "message": "El nombre de la categoría es requerido."}), 400

        category = BusinessCategory(name=name, slug=slug)
        db.session.add(category)
        db.session.commit()
        return jsonify({"status": "success", "category": category.to_dict()}), 201

    categories = BusinessCategory.query.order_by(BusinessCategory.name).all()
    return jsonify({
        "status": "success",
        "categories": [c.to_dict() for c in categories],
    }), 200
