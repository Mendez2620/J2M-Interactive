from flask import Blueprint, jsonify, request
from app.extensions import db
from app.models import Business, Staff, Customer, Transaction
from datetime import datetime, timezone

manager_bp = Blueprint('manager', __name__, url_prefix='/manager')

def get_manager_staff():
    """Utility to fetch the Staff record based on a supplied ``staff_id``.
    In a real app this would come from the session / token. For tests we
    accept a ``staff_id`` query or JSON parameter.
    """
    staff_id = request.args.get('staff_id')
    if not staff_id:
        return None, jsonify({"status": "error", "message": "staff_id requerido"}), 400
    try:
        staff_id_int = int(staff_id)
    except ValueError:
        return None, jsonify({"status": "error", "message": "staff_id invalido"}), 400
    staff = Staff.query.get(staff_id_int)
    if not staff or staff.role != 'manager':
        return None, jsonify({"status": "error", "message": "Acceso denegado: rol manager requerido"}), 403
    return staff, None, None

@manager_bp.route('/dashboard', methods=['GET'])
def manager_dashboard():
    staff, err_resp, err_code = get_manager_staff()
    if err_resp:
        return err_resp, err_code
    business = staff.business
    # Simple metrics
    total_customers = business.customers.count()
    total_transactions = business.transactions.count()
    total_stamps = sum(c.current_stamps for c in business.customers)
    total_points = sum(c.current_points for c in business.customers)
    return jsonify({
        "status": "success",
        "business": business.to_dict(),
        "metrics": {
            "customers": total_customers,
            "transactions": total_transactions,
            "stamps": total_stamps,
            "points": total_points,
        },
    }), 200

@manager_bp.route('/business/<int:business_id>/staff', methods=['GET', 'POST'])
def manage_cashiers(business_id):
    staff, err_resp, err_code = get_manager_staff()
    if err_resp:
        return err_resp, err_code
    if staff.business_id != business_id:
        return jsonify({"status": "error", "message": "Acceso a negocio no autorizado"}), 403
    if request.method == 'GET':
        cashiers = Staff.query.filter_by(business_id=business_id, role='cashier').all()
        return jsonify({
            "status": "success",
            "cashiers": [c.to_dict() for c in cashiers],
        }), 200
    # POST: create new cashier
    data = request.get_json(silent=True) or {}
    username = data.get('username')
    password = data.get('password')
    if not username or not password:
        return jsonify({"status": "error", "message": "username y password requeridos"}), 400
    new_cashier = Staff(
        business_id=business_id,
        username=username,
        name=data.get('name'),
        email=data.get('email'),
        phone=data.get('phone'),
        role='cashier',
    )
    new_cashier.set_password(password)
    db.session.add(new_cashier)
    db.session.commit()
    return jsonify({"status": "success", "cashier": new_cashier.to_dict()}), 201

@manager_bp.route('/business/<int:business_id>/export/excel', methods=['GET'])
def manager_export_excel(business_id):
    staff, err_resp, err_code = get_manager_staff()
    if err_resp:
        return err_resp, err_code
    if staff.business_id != business_id:
        return jsonify({"status": "error", "message": "Acceso no autorizado"}), 403
    from app.services import ExportService
    excel_bytes = ExportService().generate_excel_report(business_id)
    return (excel_bytes, 200, {
        'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'Content-Disposition': f'attachment; filename=business_{business_id}_report.xlsx'
    })

@manager_bp.route('/business/<int:business_id>/export/pdf', methods=['GET'])
def manager_export_pdf(business_id):
    staff, err_resp, err_code = get_manager_staff()
    if err_resp:
        return err_resp, err_code
    if staff.business_id != business_id:
        return jsonify({"status": "error", "message": "Acceso no autorizado"}), 403
    from app.services import ExportService
    pdf_bytes = ExportService().generate_pdf_report(business_id)
    return (pdf_bytes, 200, {
        'Content-Type': 'application/pdf',
        'Content-Disposition': f'attachment; filename=business_{business_id}_report.pdf'
    })

@manager_bp.route('/<int:business_id>/transactions', methods=['GET'])
def manager_transactions(business_id):
    staff, err_resp, err_code = get_manager_staff()
    if err_resp:
        return err_resp, err_code
    if staff.business_id != business_id:
        return jsonify({"status": "error", "message": "Acceso no autorizado"}), 403
    txs = Transaction.query.filter_by(business_id=business_id).order_by(Transaction.timestamp.desc()).all()
    return jsonify({
        "status": "success",
        "transactions": [t.to_dict() for t in txs],
    }), 200
