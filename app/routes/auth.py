from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from app.models import PlatformAdmin
from app.security import csrf_token
from app.services.auth_rate_limit_service import AuthRateLimitUnavailable, clear_auth_attempts, reserve_auth_attempt

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/api/auth/ping", methods=["GET"])
def ping():
    return jsonify({"message": "Auth module ready"}), 200


@auth_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        try:
            reservation = reserve_auth_attempt("admin", email, request.remote_addr)
        except AuthRateLimitUnavailable:
            current_app.logger.exception("auth_rate_limit_unavailable component=admin_login")
            return render_template("admin/login.html", error="Inicio de sesión temporalmente no disponible."), 503
        if reservation is None:
            return render_template("admin/login.html", error="Demasiados intentos. Intenta más tarde."), 429
        admin = PlatformAdmin.query.filter_by(email=email).first()
        if not admin or not admin.check_password(password):
            error = "Credenciales inválidas."
        elif not admin.is_active:
            error = "La cuenta está inactiva."
        else:
            clear_auth_attempts("admin", reservation)
            session.clear()
            session["platform_admin_id"] = admin.id
            session.permanent = True
            csrf_token()
            next_url = request.args.get("next")
            if not next_url or not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("admin.dashboard")
            return redirect(next_url)
    return render_template("admin/login.html", error=error), (401 if error else 200)


@auth_bp.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.clear()
    return redirect(url_for("auth.admin_login"))
