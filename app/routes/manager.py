from flask import Blueprint, g, jsonify, request, send_file
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Staff, Transaction
from app.security import validate_password
from app.staff_auth import staff_required

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


@manager_bp.route("/business/<int:business_id>/staff", methods=["GET", "POST"])
@staff_required("web", "manage_cashiers", roles={"manager"})
def manage_cashiers(business_id):
    denied = _own_business(business_id)
    if denied:
        return denied
    if request.method == "GET":
        cashiers = Staff.query.filter_by(business_id=business_id, role="cashier").all()
        return jsonify({"status": "success", "cashiers": [c.to_dict() for c in cashiers]}), 200
    data = request.get_json(silent=True) or {}
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
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"status": "error", "message": "El usuario ya existe en este negocio."}), 409
    return jsonify({"status": "success", "cashier": cashier.to_dict()}), 201


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
