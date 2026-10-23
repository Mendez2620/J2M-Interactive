from flask import Blueprint, current_app, g, redirect, render_template, request, url_for
from sqlalchemy import func, or_

from app.manager_auth import (
    create_staff_browser_session, revoke_current_staff_session, staff_portal_required,
)
from app.models import Business, Staff
from app.permissions import has_permission
from app.services.auth_rate_limit_service import (
    AuthRateLimitUnavailable, clear_auth_attempts, reserve_auth_attempt,
)


staff_portal_bp = Blueprint("staff_portal", __name__, url_prefix="/staff")


@staff_portal_bp.after_request
def staff_private_cache(response):
    if request.path.startswith("/staff/portal"):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
    return response


@staff_portal_bp.route("/login", methods=["GET", "POST"])
def staff_login():
    error = "La sesión de seguridad cambió. Inicia sesión nuevamente." if request.args.get("csrf_error") else None
    status = 200
    remembered_business = (request.cookies.get("j2m_staff_business") or "").strip().lower()
    if request.method == "POST":
        identity = (request.form.get("identity") or "").strip().lower()
        password = request.form.get("password") or ""
        business_slug = (request.form.get("business_slug") or "").strip().lower()
        remember = request.form.get("remember") == "1"
        try:
            reservation = reserve_auth_attempt("staff-web", f"{business_slug}:{identity}", request.remote_addr)
        except AuthRateLimitUnavailable:
            current_app.logger.exception("auth_rate_limit_unavailable component=staff_web_login")
            reservation = None
            error, status = "Inicio de sesión temporalmente no disponible.", 503
        if status == 200 and reservation is None:
            error, status = "Demasiados intentos. Intenta más tarde.", 429
        elif status == 200:
            query = Staff.query.join(Business).filter(
                or_(func.lower(Staff.email) == identity, Staff.username == identity),
                Business.slug == business_slug,
            )
            candidates = query.all() if identity and password and business_slug else []
            staff = next((item for item in candidates if item.check_password(password)), None)
            eligible = (
                staff and staff.role == "cashier" and staff.is_active and staff.business.is_active
                and staff.web_access_enabled and staff.business.web_access_enabled
                and has_permission(staff, "scanner_operate_web")
            )
            if not eligible:
                error, status = "No fue posible iniciar sesión con esas credenciales.", 401
            else:
                clear_auth_attempts("staff-web", reservation)
                create_staff_browser_session(staff, remember=remember)
                response = redirect(url_for("staff_portal.staff_scanner"))
                if remember:
                    response.set_cookie(
                        "j2m_staff_business", business_slug,
                        max_age=current_app.config["REMEMBER_SESSION_ABSOLUTE_SECONDS"],
                        httponly=True, secure=current_app.config["SESSION_COOKIE_SECURE"],
                        samesite="Lax",
                    )
                else:
                    response.delete_cookie("j2m_staff_business", samesite="Lax")
                return response
    return render_template(
        "staff/login.html", error=error, remembered_business=remembered_business,
    ), status


@staff_portal_bp.route("/logout", methods=["POST"])
def staff_logout():
    revoke_current_staff_session()
    return redirect(url_for("staff_portal.staff_login"))


@staff_portal_bp.route("/portal/scanner", methods=["GET"])
@staff_portal_required("scanner_operate_web")
def staff_scanner():
    return render_template("staff/scanner.html", staff=g.staff_browser, business=g.staff_browser.business)
