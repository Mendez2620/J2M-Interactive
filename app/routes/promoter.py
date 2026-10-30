from datetime import datetime, timedelta, timezone
from flask import Blueprint, current_app, g, redirect, render_template, request, send_file, session, url_for
from io import BytesIO
from reportlab.graphics import renderSVG
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from urllib.parse import urlparse

from app.access_audit import access_audit
from app.extensions import db
from app.models import Business, Promoter, ReferralAttribution, ReferralEligibility
from app.phone_numbers import normalize_phone
from app.promoter_auth import (
    create_promoter_session, promoter_required, revoke_current_promoter_session,
    revoke_promoter_sessions,
)
from app.security import current_platform_admin, platform_admin_required, valid_email, validate_password
from app.services.auth_rate_limit_service import AuthRateLimitUnavailable, clear_auth_attempts, reserve_auth_attempt
from app.services.referral_participant_service import ensure_external_promoter


promoter_bp = Blueprint("promoter", __name__)


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _referral_url(promoter):
    configured = (current_app.config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    parsed = urlparse(configured)
    valid_configured = bool(
        parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
        and not parsed.password and not parsed.query and not parsed.fragment
        and parsed.path in {"", "/"}
    )
    if current_app.config.get("APP_ENV") == "production":
        if not valid_configured or parsed.scheme != "https":
            raise RuntimeError("El origen público canónico no está configurado de forma segura.")
        base = configured
    elif valid_configured:
        base = configured
    else:
        request_origin = urlparse(request.url_root)
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        base = request.url_root.rstrip("/") if request_origin.hostname in local_hosts else "http://localhost"
    return f"{base}/join/{promoter.business.slug}?ref={promoter.referral_code}"


@promoter_bp.after_request
def private_promoter_pages(response):
    if request.path.startswith("/promoter/portal") or request.path.startswith("/admin/business/") and "/promoters" in request.path:
        response.headers["Cache-Control"] = "no-store, private"
    return response


@promoter_bp.route("/promoter/login", methods=["GET", "POST"])
def promoter_login():
    error, status = None, 200
    if request.method == "POST":
        identity = (request.form.get("identity") or "").strip().lower()
        slug = (request.form.get("business_slug") or "").strip().lower()
        password = request.form.get("password") or ""
        try:
            reservation = reserve_auth_attempt("promoter-web", f"{slug}:{identity}", request.remote_addr)
        except AuthRateLimitUnavailable:
            current_app.logger.exception("auth_rate_limit_unavailable component=promoter_login")
            reservation, error, status = None, "Inicio de sesión temporalmente no disponible.", 503
        if status == 200 and reservation is None:
            error, status = "Demasiados intentos. Intenta más tarde.", 429
        elif status == 200:
            candidates = Promoter.query.join(Business).filter(
                Business.slug == slug,
                or_(func.lower(Promoter.username) == identity, func.lower(Promoter.email) == identity),
            ).all() if identity and slug and password else []
            promoter = next((item for item in candidates if item.check_password(password)), None)
            if not promoter or not promoter.is_active or not promoter.business.is_active:
                error, status = "No fue posible iniciar sesión con esas credenciales.", 401
            else:
                clear_auth_attempts("promoter-web", reservation)
                create_promoter_session(promoter)
                return redirect(url_for("promoter.promoter_portal"))
    return render_template("promoter/login.html", error=error), status


@promoter_bp.route("/promoter/logout", methods=["POST"])
def promoter_logout():
    revoke_current_promoter_session()
    return redirect(url_for("promoter.promoter_login"))


@promoter_bp.route("/promoter/portal")
@promoter_required
def promoter_portal():
    promoter = g.promoter
    verified = ReferralEligibility.query.filter_by(
        promoter_id=promoter.id, business_id=promoter.business_id, status="verified"
    )
    total = verified.count()
    month_start = _utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month = verified.filter(ReferralEligibility.reviewed_at >= month_start).count()
    return render_template("promoter/portal.html", promoter=promoter, total=total, month=month, referral_url=_referral_url(promoter))


@promoter_bp.route("/promoter/referral-qr.svg")
@promoter_required
def promoter_qr():
    return _qr_response(g.promoter)


def _qr_response(promoter):
    qr = QrCodeWidget(_referral_url(promoter))
    bounds = qr.getBounds()
    drawing = Drawing(240, 240, transform=[240 / (bounds[2] - bounds[0]), 0, 0, 240 / (bounds[3] - bounds[1]), 0, 0])
    drawing.add(qr)
    return send_file(
        BytesIO(renderSVG.drawToString(drawing).encode("utf-8")),
        mimetype="image/svg+xml", download_name="codigo-referido.svg",
    )


@promoter_bp.route("/admin/business/<int:business_id>/promoters/<int:promoter_id>/qr.svg")
@platform_admin_required
def admin_promoter_qr(business_id, promoter_id):
    promoter = Promoter.query.filter_by(id=promoter_id, business_id=business_id).first_or_404()
    return _qr_response(promoter)


@promoter_bp.route("/admin/business/<int:business_id>/promoters", methods=["GET", "POST"])
@platform_admin_required
def admin_promoters(business_id):
    business = db.session.get(Business, business_id)
    if not business:
        return {"status": "error", "message": "Negocio no encontrado."}, 404
    if request.method == "POST":
        return {
            "status": "error",
            "message": "La creación de promotores externos ya no está disponible.",
        }, 410
    return redirect(url_for("admin.get_business_details", business_id=business.id, tab="referrals"))


@promoter_bp.route("/admin/business/<int:business_id>/promoters/<int:promoter_id>", methods=["POST"])
@platform_admin_required
def update_promoter(business_id, promoter_id):
    promoter = Promoter.query.filter_by(id=promoter_id, business_id=business_id).first_or_404()
    action = request.form.get("action")
    previous = {"is_active": promoter.is_active, "name": promoter.name}
    if action == "toggle":
        audit_action = "promoter_suspended" if promoter.is_active else "promoter_activated"
        promoter.is_active = not promoter.is_active
        revoke_promoter_sessions(promoter)
    elif action == "password":
        audit_action = "promoter_password_reset"
        password = request.form.get("password") or ""
        error = validate_password(password)
        if error:
            return {"status": "error", "message": error}, 400
        promoter.set_password(password)
        revoke_promoter_sessions(promoter)
    elif action == "edit":
        audit_action = "promoter_edited"
        name = (request.form.get("name") or "").strip()
        email = (request.form.get("email") or "").strip().lower() or None
        if not 2 <= len(name) <= 120 or email and not valid_email(email):
            return {"status": "error", "message": "Nombre o correo no válido."}, 400
        promoter.name, promoter.email = name, email
    else:
        return {"status": "error", "message": "Acción no válida."}, 400
    db.session.add(access_audit(
        audit_action, business_id=business_id,
        platform_admin_id=current_platform_admin().id,
        previous={**previous, "promoter_id": promoter.id},
        new={"promoter_id": promoter.id, "name": promoter.name, "is_active": promoter.is_active, "action": action},
    ))
    db.session.commit()
    return redirect(url_for("promoter.admin_promoters", business_id=business_id))
