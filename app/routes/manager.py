from flask import Blueprint, g, jsonify, request, send_file
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Customer, Staff, Transaction
from app.security import valid_email, validate_password
from app.staff_auth import revoke_staff_sessions, staff_required
from app.access_audit import access_audit, staff_access_state

manager_bp = Blueprint("manager", __name__, url_prefix="/manager")


def _own_business(business_id):
    if g.staff.business_id != business_id:
        return jsonify({"status": "error", "message": "Acceso a negocio no autorizado."}), 403


@manager_bp.route("/dashboard", methods=["GET"])
@staff_required("web", "view_stats", roles={"manager"})
def manager_dashboard():
    business = g.staff.business
    return jsonify({"status": "success", "business": business.to_dict(), "metrics": {
        "customers": business.customers.count(), "transactions": business.transactions.count(),
        "stamps": sum(c.current_stamps for c in business.customers),
        "points": sum(c.current_points for c in business.customers)}}), 200


@manager_bp.route("/business/<int:business_id>/staff", methods=["GET"])
@staff_required("web", "view_cashiers", roles={"manager"})
def list_cashiers(business_id):
    denied = _own_business(business_id)
    if denied:
        return denied
    cashiers = Staff.query.filter_by(business_id=business_id, role="cashier").order_by(Staff.created_at.desc()).all()
    return jsonify({"status": "success", "cashiers": [c.to_dict() for c in cashiers]}), 200


@manager_bp.route("/business/<int:business_id>/staff", methods=["POST"])
@staff_required("web", "create_cashiers", roles={"manager"})
def create_cashier(business_id):
    denied = _own_business(business_id)
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    if data.get("role") not in {None, "cashier"} or "permissions" in data or "business_id" in data:
        return jsonify({"status": "error", "message": "Solo se permite crear cajeros sin privilegios configurables."}), 400
    username, password = (data.get("username") or "").strip(), data.get("password") or ""
    password_error = validate_password(password)
    if not username or password_error:
        return jsonify({"status": "error", "message": password_error or "username requerido"}), 400
    cashier = Staff(business_id=business_id, username=username, name=data.get("name"),
                    email=data.get("email"), phone=data.get("phone"), role="cashier",
                    permissions="scanner_operate", mobile_access_enabled=False, web_access_enabled=False)
    cashier.set_password(password)
    try:
        db.session.add(cashier)
        db.session.flush()
        db.session.add(access_audit(
            "staff_created", cashier,
            new=staff_access_state(cashier) | {"role": cashier.role},
            actor_staff_id=g.staff.id,
        ))
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"status": "error", "message": "El usuario ya existe en este negocio."}), 409
    return jsonify({"status": "success", "cashier": cashier.to_dict()}), 201


def _own_cashier(business_id, staff_id):
    denied = _own_business(business_id)
    if denied:
        return None, denied
    cashier = db.session.get(Staff, staff_id)
    if not cashier or cashier.business_id != business_id or cashier.role != "cashier":
        return None, (jsonify({"status": "error", "message": "Cajero no encontrado."}), 404)
    return cashier, None


@manager_bp.route("/business/<int:business_id>/staff/<int:staff_id>", methods=["PATCH"])
@staff_required("web", "edit_cashiers", roles={"manager"})
def edit_cashier(business_id, staff_id):
    cashier, denied = _own_cashier(business_id, staff_id)
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    if set(data) - {"name", "email", "phone"}:
        return jsonify({"status": "error", "message": "Solo pueden editarse nombre, correo y teléfono."}), 400
    email = (data.get("email") or "").strip() or None
    if email and not valid_email(email):
        return jsonify({"status": "error", "message": "Correo electrónico inválido."}), 400
    if "name" in data:
        cashier.name = (data.get("name") or "").strip() or None
    if "email" in data:
        cashier.email = email
    if "phone" in data:
        cashier.phone = (data.get("phone") or "").strip() or None
    db.session.commit()
    return jsonify({"status": "success", "cashier": cashier.to_dict()}), 200


@manager_bp.route("/business/<int:business_id>/staff/<int:staff_id>/toggle-status", methods=["POST"])
@staff_required("web", "toggle_cashiers", roles={"manager"})
def toggle_cashier(business_id, staff_id):
    cashier, denied = _own_cashier(business_id, staff_id)
    if denied:
        return denied
    previous = staff_access_state(cashier)
    cashier.is_active = not cashier.is_active
    revoke_staff_sessions(cashier)
    db.session.add(access_audit(
        "cashier_status_updated", cashier, previous, staff_access_state(cashier),
        actor_staff_id=g.staff.id,
    ))
    db.session.commit()
    return jsonify({"status": "success", "cashier": cashier.to_dict()}), 200


@manager_bp.route("/business/<int:business_id>/staff/<int:staff_id>/reset-password", methods=["POST"])
@staff_required("web", "reset_cashier_password", roles={"manager"})
def reset_cashier_password(business_id, staff_id):
    cashier, denied = _own_cashier(business_id, staff_id)
    if denied:
        return denied
    data = request.get_json(silent=True) or {}
    password = data.get("password") or ""
    if password != (data.get("password_confirmation") or ""):
        return jsonify({"status": "error", "message": "Las contraseñas no coinciden."}), 400
    password_error = validate_password(password)
    if password_error:
        return jsonify({"status": "error", "message": password_error}), 400
    cashier.set_password(password)
    revoke_staff_sessions(cashier)
    db.session.add(access_audit(
        "password_reset", cashier, actor_staff_id=g.staff.id,
        new={"password_reset": True},
    ))
    db.session.commit()
    return jsonify({"status": "success", "message": "Contraseña restablecida."}), 200


@manager_bp.route("/business/<int:business_id>/customers", methods=["GET"])
@staff_required("web", "view_customers", roles={"manager"})
def manager_customers(business_id):
    denied = _own_business(business_id)
    if denied:
        return denied
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 20, type=int), 1), 100)
    pagination = Customer.query.filter_by(business_id=business_id).order_by(Customer.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    customers = [{
        "id": customer.id, "full_name": customer.full_name, "email": customer.email,
        "phone": customer.phone, "current_points": customer.current_points,
        "current_stamps": customer.current_stamps,
    } for customer in pagination.items]
    return jsonify({"status": "success", "customers": customers, "pagination": {
        "page": pagination.page, "pages": pagination.pages, "total": pagination.total,
        "per_page": pagination.per_page,
    }}), 200


@manager_bp.route("/business/<int:business_id>/export/excel", methods=["GET"])
@staff_required("web", "export_reports", roles={"manager"})
def manager_export_excel(business_id):
    denied = _own_business(business_id)
    if denied:
        return denied
    from app.services import ExportService
    business = g.staff.business
    report = ExportService.generate_excel_report(business)
    return send_file(
        report,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"{business.slug}-reporte.xlsx",
    )


@manager_bp.route("/business/<int:business_id>/export/pdf", methods=["GET"])
@staff_required("web", "export_reports", roles={"manager"})
def manager_export_pdf(business_id):
    denied = _own_business(business_id)
    if denied:
        return denied
    from app.services import ExportService
    business = g.staff.business
    report = ExportService.generate_pdf_report(business)
    return send_file(
        report,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{business.slug}-reporte.pdf",
    )


@manager_bp.route("/<int:business_id>/transactions", methods=["GET"])
@staff_required("web", "view_transactions", roles={"manager"})
def manager_transactions(business_id):
    denied = _own_business(business_id)
    if denied:
        return denied
    txs = Transaction.query.filter_by(business_id=business_id).order_by(Transaction.timestamp.desc()).all()
    return jsonify({"status": "success", "transactions": [t.to_dict() for t in txs]}), 200
