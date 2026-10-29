from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Business, Customer, Staff, Transaction
from app.security import valid_email, validate_password, validate_csrf
from app.staff_auth import revoke_staff_sessions, staff_required
from app.access_audit import access_audit, staff_access_state
from app.manager_auth import create_manager_browser_session, manager_portal_required, revoke_current_manager_session
from app.permissions import effective_permissions, has_permission
from app.services.auth_rate_limit_service import AuthRateLimitUnavailable, clear_auth_attempts, reserve_auth_attempt
from app.services.unified_staff_access_service import bounded_password_matches, unified_staff_access
from app.services.referral_eligibility_service import (
    ReferralEligibilityError, decide_referral_eligibility,
)

manager_bp = Blueprint("manager", __name__, url_prefix="/manager")
# Legacy test hook; authentication throttling is persisted in auth_rate_limit_slots.
_portal_login_attempts = {}


@manager_bp.after_request
def manager_private_cache(response):
    if request.path.startswith("/manager/portal"):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
    return response


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
    email = (data.get("email") or "").strip().lower() or None
    if email and not valid_email(email):
        return jsonify({"status": "error", "message": "Correo electrónico inválido."}), 400
    cashier = Staff(business_id=business_id, username=username, name=data.get("name"),
                    email=email, phone=data.get("phone"), role="cashier",
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
    email = (data.get("email") or "").strip().lower() or None
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


@manager_bp.post("/business/<int:business_id>/referrals/<int:eligibility_id>/decision")
@staff_required("web", "review_referrals", roles={"manager"})
def review_referral_eligibility(business_id, eligibility_id):
    if not validate_csrf():
        return jsonify({"status": "error", "message": "Token CSRF inválido o ausente."}), 400
    return _apply_referral_decision(g.staff, business_id, eligibility_id)


@manager_bp.post("/portal/referrals/<int:eligibility_id>/decision")
@manager_portal_required("review_referrals")
def portal_review_referral_eligibility(eligibility_id):
    """Cookie-session integration point; listing/UI remains reserved for 8C.3."""
    return _apply_referral_decision(g.manager, g.manager.business_id, eligibility_id)


def _apply_referral_decision(actor, business_id, eligibility_id):
    if actor.business_id != business_id:
        return jsonify({"status": "error", "message": "Acceso a negocio no autorizado."}), 403
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) - {
        "status", "transaction_id", "evidence_reference", "evidence_type",
        "reason", "request_id"
    }:
        return jsonify({"status": "error", "message": "Solicitud de revisión inválida."}), 400
    request_id = request.headers.get("Idempotency-Key") or data.get("request_id")
    try:
        result = decide_referral_eligibility(
            business=actor.business, actor=actor, eligibility_id=eligibility_id,
            to_status=data.get("status"), transaction_id=data.get("transaction_id"),
            evidence_reference=data.get("evidence_reference"),
            evidence_type=data.get("evidence_type"), reason=data.get("reason"),
            request_id=request_id,
        )
    except ReferralEligibilityError as exc:
        return jsonify({"status": "error", "message": str(exc)}), exc.status_code
    return jsonify({
        "status": "success", "eligibility": {
            "id": result.eligibility.id, "status": result.eligibility.status,
            "updated_at": result.eligibility.updated_at.isoformat(),
        }, "idempotent_replay": result.replayed,
    }), 200


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


def _portal_destination(staff):
    permissions = effective_permissions(staff)
    for permission, endpoint in (
        ("scanner_operate_web", "manager.portal_scanner"),
        ("view_stats", "manager.manager_portal"),
        ("view_customers", "manager.portal_customers"),
        ("view_transactions", "manager.portal_transactions"),
        ("export_reports", "manager.portal_reports"),
        ("view_cashiers", "manager.portal_cashiers"),
        ("create_cashiers", "manager.portal_cashiers"),
        ("edit_cashiers", "manager.portal_cashiers"),
        ("toggle_cashiers", "manager.portal_cashiers"),
        ("reset_cashier_password", "manager.portal_cashiers"),
    ):
        if permission in permissions:
            return url_for(endpoint)
    return url_for("manager.manager_account")


@manager_bp.route("/login", methods=["GET", "POST"])
def manager_login():
    if request.method == "GET":
        return unified_staff_access()
    error = "La sesión de seguridad cambió. Inicia sesión nuevamente." if request.args.get("csrf_error") else None
    status = 200
    account_choices = None
    if request.method == "POST":
        pending = session.get("manager_account_choice") or {}
        account_id = request.form.get("account_id", type=int)
        pending_accounts = {
            item.get("staff_id"): item.get("auth_version")
            for item in pending.get("accounts", [])
            if isinstance(item, dict)
        }
        if account_id and pending.get("expires_at", 0) >= datetime.now(timezone.utc).timestamp() and account_id in pending_accounts:
            staff = db.session.get(Staff, account_id)
            if (staff and staff.role == "manager" and staff.is_active and staff.business.is_active
                    and staff.web_access_enabled and staff.business.web_access_enabled
                    and staff.auth_version == pending_accounts[account_id]):
                session.pop("manager_account_choice", None)
                create_manager_browser_session(staff, remember=bool(pending.get("remember")))
                return redirect(_portal_destination(staff))
            session.pop("manager_account_choice", None)
            error, status = "No fue posible iniciar sesión con esas credenciales.", 401
            return render_template(
                "staff/access.html", error=error, account_choices=None,
                account_form_action=url_for("manager.manager_login"),
            ), status

        # Any credential submission supersedes an earlier account choice.
        session.pop("manager_account_choice", None)
        session.pop("staff_access_choice", None)
        # ``identity`` remains a compatibility input for older web clients; the
        # current portal deliberately requests email only.
        identity = (request.form.get("email") or request.form.get("identity") or "").strip().lower()
        password = request.form.get("password") or ""
        remember = request.form.get("remember") == "1"
        business_slug = (request.form.get("business_slug") or "").strip()
        rate_identity = identity if valid_email(identity) else f"{business_slug}:{identity}"
        try:
            reservation = reserve_auth_attempt("staff-access", rate_identity, request.remote_addr)
        except AuthRateLimitUnavailable:
            current_app.logger.exception("auth_rate_limit_unavailable component=manager_login")
            error, status, reservation = "Inicio de sesión temporalmente no disponible.", 503, None
        if status == 200 and reservation is None:
            error, status = "Demasiados intentos. Intenta más tarde.", 429
        elif status == 200:
            eligible = bounded_password_matches(
                identity, password, allowed_roles={"manager"},
                business_slug=business_slug or None, allow_username=True,
            )
            if not eligible:
                error, status = "No fue posible iniciar sesión con esas credenciales.", 401
            elif len(eligible) > 1 and not business_slug:
                clear_auth_attempts("staff-access", reservation)
                session["manager_account_choice"] = {
                    "accounts": [
                        {"staff_id": item.id, "auth_version": item.auth_version}
                        for item in eligible
                    ],
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp(),
                    "remember": remember,
                }
                account_choices = eligible
            else:
                clear_auth_attempts("staff-access", reservation)
                staff = eligible[0]
                create_manager_browser_session(staff, remember=remember)
                return redirect(_portal_destination(staff))
    return render_template(
        "staff/access.html", error=error, account_choices=account_choices,
        account_form_action=url_for("manager.manager_login"),
    ), status


@manager_bp.route("/logout", methods=["POST"])
def manager_logout():
    revoke_current_manager_session()
    return redirect(url_for("main.staff_access"))


def _portal_context(active):
    return {"manager": g.manager, "business": g.manager.business,
            "manager_permissions": g.manager_permissions, "active": active}


@manager_bp.route("/portal", methods=["GET"])
@manager_portal_required("view_stats")
def manager_portal():
    requested_period = request.args.get("period", "7")
    period = int(requested_period) if requested_period in {"1", "7", "30"} else 7
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    since = now - timedelta(days=period)
    business = g.manager.business
    tx_query = Transaction.query.filter(Transaction.business_id == business.id, Transaction.timestamp >= since)
    transactions = []
    if "view_transactions" in g.manager_permissions:
        transactions = tx_query.order_by(Transaction.timestamp.desc()).limit(8).all()
    totals = db.session.query(
        func.count(Transaction.id), func.coalesce(func.sum(Transaction.points_amount), 0),
        func.coalesce(func.sum(Transaction.stamps_amount), 0), func.coalesce(func.sum(Transaction.purchase_amount), 0),
    ).filter(Transaction.business_id == business.id, Transaction.timestamp >= since).one()
    earned_points = db.session.query(func.coalesce(func.sum(Transaction.points_amount), 0)).filter_by(business_id=business.id, type="earn").filter(Transaction.timestamp >= since).scalar()
    redeemed_points = db.session.query(func.coalesce(func.sum(Transaction.points_amount), 0)).filter_by(business_id=business.id, type="redeem").filter(Transaction.timestamp >= since).scalar()
    earned_stamps = db.session.query(func.coalesce(func.sum(Transaction.stamps_amount), 0)).filter_by(business_id=business.id, type="earn").filter(Transaction.timestamp >= since).scalar()
    redeemed_stamps = db.session.query(func.coalesce(func.sum(Transaction.stamps_amount), 0)).filter_by(business_id=business.id, type="redeem").filter(Transaction.timestamp >= since).scalar()
    timestamps = [row[0] for row in tx_query.with_entities(Transaction.timestamp).all()]
    if period == 1:
        first_bucket = since.replace(minute=0, second=0, microsecond=0)
        chart_labels = [(first_bucket + timedelta(hours=offset)).strftime("%H:00") for offset in range(25)]
        chart_values = [0] * len(chart_labels)
        for value in timestamps:
            bucket_index = int((value - first_bucket).total_seconds() // 3600)
            if 0 <= bucket_index < len(chart_values):
                chart_values[bucket_index] += 1
    else:
        first_bucket = since.date()
        chart_labels = [(first_bucket + timedelta(days=offset)).strftime("%d/%m") for offset in range(period + 1)]
        chart_values = [0] * len(chart_labels)
        for value in timestamps:
            bucket_index = (value.date() - first_bucket).days
            if 0 <= bucket_index < len(chart_values):
                chart_values[bucket_index] += 1
    metrics = {
        "customers": business.customers.count(), "transactions": totals[0],
        "current_points": db.session.query(func.coalesce(func.sum(Customer.current_points), 0)).filter_by(business_id=business.id).scalar(),
        "current_stamps": db.session.query(func.coalesce(func.sum(Customer.current_stamps), 0)).filter_by(business_id=business.id).scalar(),
        "earned_points": earned_points, "redeemed_points": redeemed_points,
        "earned_stamps": earned_stamps, "redeemed_stamps": redeemed_stamps,
        "purchases_recorded": totals[3],
        "new_customers": Customer.query.filter(Customer.business_id == business.id, Customer.created_at >= since).count(),
        "rewards_available": Customer.query.filter(Customer.business_id == business.id, Customer.current_stamps >= business.stamps_reward_limit).count() if business.loyalty_type == "stamps" else 0,
    }
    return render_template("manager/dashboard.html", metrics=metrics, transactions=transactions,
                           chart_labels=chart_labels, chart_values=chart_values, period=period,
                           **_portal_context("dashboard"))


@manager_bp.route("/portal/scanner", methods=["GET"])
@manager_portal_required("scanner_operate_web")
def portal_scanner():
    return render_template("manager/scanner.html", **_portal_context("scanner"))


@manager_bp.route("/portal/customers", methods=["GET"])
@manager_portal_required("view_customers")
def portal_customers():
    page = max(request.args.get("page", 1, type=int), 1)
    search = (request.args.get("q") or "").strip()
    query = Customer.query.filter_by(business_id=g.manager.business_id)
    if search:
        like = f"%{search}%"
        query = query.filter(or_(Customer.full_name.ilike(like), Customer.email.ilike(like), Customer.phone.ilike(like)))
    pagination = query.order_by(Customer.created_at.desc()).paginate(page=page, per_page=20, error_out=False)
    return render_template("manager/customers.html", pagination=pagination, search=search, **_portal_context("customers"))


@manager_bp.route("/portal/transactions", methods=["GET"])
@manager_portal_required("view_transactions")
def portal_transactions():
    page = max(request.args.get("page", 1, type=int), 1)
    movement = (request.args.get("type") or "").strip()
    query = Transaction.query.filter_by(business_id=g.manager.business_id)
    if movement in {"earn", "redeem"}:
        query = query.filter_by(type=movement)
    for field, operator in (("from", ">="), ("to", "<")):
        raw = request.args.get(field)
        if raw:
            try:
                value = datetime.strptime(raw, "%Y-%m-%d") + (timedelta(days=1) if field == "to" else timedelta())
                query = query.filter(Transaction.timestamp < value if operator == "<" else Transaction.timestamp >= value)
            except ValueError:
                pass
    pagination = query.order_by(Transaction.timestamp.desc()).paginate(page=page, per_page=25, error_out=False)
    return render_template("manager/transactions.html", pagination=pagination, movement=movement, **_portal_context("transactions"))


@manager_bp.route("/portal/reports", methods=["GET"])
@manager_portal_required("export_reports")
def portal_reports():
    return render_template("manager/reports.html", **_portal_context("reports"))


@manager_bp.route("/portal/reports/<kind>", methods=["GET"])
@manager_portal_required("export_reports")
def portal_report_download(kind):
    from app.services import ExportService
    business = g.manager.business
    if kind == "excel":
        return send_file(ExportService.generate_excel_report(business), mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True, download_name=f"{business.slug}-reporte.xlsx")
    if kind == "pdf":
        return send_file(ExportService.generate_pdf_report(business), mimetype="application/pdf", as_attachment=True, download_name=f"{business.slug}-reporte.pdf")
    return render_template("manager/error.html", message="Formato de reporte no disponible.", **_portal_context("reports")), 404


@manager_bp.route("/portal/cashiers", methods=["GET"])
@manager_portal_required()
def portal_cashiers():
    cashier_permissions = {"view_cashiers", "create_cashiers", "edit_cashiers", "toggle_cashiers", "reset_cashier_password"}
    if not (cashier_permissions & g.manager_permissions):
        return redirect(url_for("manager.manager_account", denied="cashiers"))
    fields = {"id"}
    if "view_cashiers" in g.manager_permissions:
        fields.update({"username", "name", "email", "phone", "is_active"})
    else:
        if {"edit_cashiers", "toggle_cashiers", "reset_cashier_password"} & g.manager_permissions:
            fields.update({"username", "name"})
        if "edit_cashiers" in g.manager_permissions:
            fields.update({"email", "phone"})
        if "toggle_cashiers" in g.manager_permissions:
            fields.add("is_active")
    cashiers = []
    if fields != {"id"}:
        columns = [getattr(Staff, field) for field in sorted(fields)]
        rows = db.session.query(*columns).filter(Staff.business_id == g.manager.business_id, Staff.role == "cashier").order_by(Staff.created_at.desc()).all()
        cashiers = [dict(row._mapping) for row in rows]
    return render_template("manager/cashiers.html", cashiers=cashiers, **_portal_context("cashiers"))


@manager_bp.route("/portal/cashiers/create", methods=["POST"])
@manager_portal_required("create_cashiers")
def portal_create_cashier():
    data = request.form
    username, password = (data.get("username") or "").strip(), data.get("password") or ""
    error = validate_password(password)
    if not username or error:
        flash(error or "El usuario es obligatorio.", "error")
        return redirect(url_for("manager.portal_cashiers"))
    email = (data.get("email") or "").strip().lower() or None
    if email and not valid_email(email):
        flash("Correo electrónico inválido.", "error")
        return redirect(url_for("manager.portal_cashiers"))
    cashier = Staff(business_id=g.manager.business_id, username=username, name=(data.get("name") or "").strip() or None,
                    email=email, phone=(data.get("phone") or "").strip() or None,
                    role="cashier", permissions="scanner_operate", mobile_access_enabled=False, web_access_enabled=False)
    cashier.set_password(password)
    try:
        db.session.add(cashier); db.session.flush()
        db.session.add(access_audit("staff_created", cashier, new=staff_access_state(cashier) | {"role": "cashier"}, actor_staff_id=g.manager.id))
        db.session.commit(); flash("Cajero creado correctamente.", "success")
    except IntegrityError:
        db.session.rollback(); flash("El usuario ya existe en este negocio.", "error")
    return redirect(url_for("manager.portal_cashiers"))


@manager_bp.route("/portal/cashiers/<int:staff_id>/edit", methods=["POST"])
@manager_portal_required("edit_cashiers")
def portal_edit_cashier(staff_id):
    cashier = Staff.query.filter_by(id=staff_id, business_id=g.manager.business_id, role="cashier").first_or_404()
    cashier.name = (request.form.get("name") or "").strip() or None
    email = (request.form.get("email") or "").strip().lower() or None
    if email and not valid_email(email):
        flash("Correo electrónico inválido.", "error"); return redirect(url_for("manager.portal_cashiers"))
    cashier.email, cashier.phone = email, (request.form.get("phone") or "").strip() or None
    db.session.commit(); flash("Datos del cajero actualizados.", "success")
    return redirect(url_for("manager.portal_cashiers"))


@manager_bp.route("/portal/cashiers/<int:staff_id>/toggle", methods=["POST"])
@manager_portal_required("toggle_cashiers")
def portal_toggle_cashier(staff_id):
    cashier = Staff.query.filter_by(id=staff_id, business_id=g.manager.business_id, role="cashier").first_or_404()
    previous = staff_access_state(cashier); cashier.is_active = not cashier.is_active; revoke_staff_sessions(cashier)
    db.session.add(access_audit("cashier_status_updated", cashier, previous, staff_access_state(cashier), actor_staff_id=g.manager.id))
    db.session.commit(); flash("Estado del cajero actualizado.", "success")
    return redirect(url_for("manager.portal_cashiers"))


@manager_bp.route("/portal/cashiers/<int:staff_id>/reset-password", methods=["POST"])
@manager_portal_required("reset_cashier_password")
def portal_reset_cashier_password(staff_id):
    cashier = Staff.query.filter_by(id=staff_id, business_id=g.manager.business_id, role="cashier").first_or_404()
    password = request.form.get("password") or ""
    if password != (request.form.get("password_confirmation") or "") or validate_password(password):
        flash(validate_password(password) or "Las contraseñas no coinciden.", "error"); return redirect(url_for("manager.portal_cashiers"))
    cashier.set_password(password); revoke_staff_sessions(cashier)
    db.session.add(access_audit("password_reset", cashier, actor_staff_id=g.manager.id, new={"password_reset": True}))
    db.session.commit(); flash("Contraseña restablecida.", "success")
    return redirect(url_for("manager.portal_cashiers"))


@manager_bp.route("/portal/account", methods=["GET"])
@manager_portal_required()
def manager_account():
    return render_template("manager/account.html", denied=request.args.get("denied"), **_portal_context("account"))
