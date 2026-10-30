from urllib.parse import urlparse

from flask import Blueprint, current_app, g, jsonify, redirect, render_template, request, session, url_for
from reportlab.graphics import renderSVG
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.graphics.shapes import Drawing
from sqlalchemy import func, or_
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.manager_auth import (
    create_staff_browser_session, revoke_current_staff_session, staff_portal_required,
)
from app.models import Business, ReferralAttribution, ReferralEligibility, ReferralParticipant, Staff
from app.permissions import has_permission
from app.services.auth_rate_limit_service import (
    AuthRateLimitUnavailable, clear_auth_attempts, reserve_auth_attempt,
)
from app.services.unified_staff_access_service import unified_staff_access
from app.services.referral_participant_service import staff_can_issue


staff_portal_bp = Blueprint("staff_portal", __name__, url_prefix="/staff")
_LEGACY_DUMMY_PASSWORD_HASH = generate_password_hash("J2M-legacy-invalid-credential-padding-only")


@staff_portal_bp.after_request
def staff_private_cache(response):
    if request.path.startswith("/staff/portal"):
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
    return response


@staff_portal_bp.route("/login", methods=["GET", "POST"])
def staff_login():
    if request.method == "GET" and request.args.get("legacy") != "1":
        return unified_staff_access()
    error = "La sesión de seguridad cambió. Inicia sesión nuevamente." if request.args.get("csrf_error") else None
    status = 200
    remembered_business = (request.cookies.get("j2m_staff_business") or "").strip().lower()
    if request.method == "POST":
        session.pop("staff_access_choice", None)
        session.pop("manager_account_choice", None)
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
            staff = Staff.query.join(Business).filter(
                Staff.username == identity,
                Business.slug == business_slug,
                or_(Staff.email.is_(None), Staff.email == ""),
            ).first() if identity and password and business_slug else None
            password_hash = staff.password_hash if staff else _LEGACY_DUMMY_PASSWORD_HASH
            if not check_password_hash(password_hash, password):
                staff = None
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
    return redirect(url_for("main.staff_access"))


@staff_portal_bp.route("/portal/scanner", methods=["GET"])
@staff_portal_required("scanner_operate_web")
def staff_scanner():
    participant = _staff_invitation()
    return render_template(
        "staff/scanner.html", staff=g.staff_browser, business=g.staff_browser.business,
        can_refer=staff_can_issue(participant),
        can_link_customers=has_permission(g.staff_browser, "authorize_customer_identity"),
    )


def _staff_invitation():
    return ReferralParticipant.query.filter_by(
        business_id=g.staff_browser.business_id, staff_id=g.staff_browser.id,
        participant_type="staff", is_enabled=True,
    ).first()


def _invitation_url(participant):
    base = (current_app.config.get("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    parsed = urlparse(base)
    valid = bool(
        parsed.scheme in {"http", "https"} and parsed.hostname
        and not parsed.username and not parsed.password
        and not parsed.query and not parsed.fragment and parsed.path in {"", "/"}
    )
    if current_app.config.get("APP_ENV") == "production":
        if not valid or parsed.scheme != "https":
            return None
    elif not valid:
        request_origin = urlparse(request.url_root)
        base = request.url_root.rstrip("/") if request_origin.hostname in {"localhost", "127.0.0.1", "::1"} else "http://localhost"
    path = url_for(
        "business.join_business", slug=g.staff_browser.business.slug,
        ref=participant.invitation_code,
    )
    return f"{base}{path}"


@staff_portal_bp.get("/portal/referral-invitation")
@staff_portal_required("refer_customers")
def staff_referral_invitation():
    participant = _staff_invitation()
    if not staff_can_issue(participant):
        return jsonify({"status": "error", "message": "Los referidos no están habilitados."}), 403
    invitation_url = _invitation_url(participant)
    if not invitation_url:
        return jsonify({"status": "error", "message": "El origen público no está configurado."}), 503
    return jsonify({
        "status": "success", "invitation_code": participant.invitation_code,
        "invitation_path": url_for(
            "business.join_business", slug=g.staff_browser.business.slug,
            ref=participant.invitation_code,
        ),
        "invitation_url": invitation_url,
    }), 200


@staff_portal_bp.get("/portal/referrals")
@staff_portal_required("refer_customers")
def staff_referrals():
    participant = _staff_invitation()
    if not staff_can_issue(participant):
        return render_template(
            "staff/referrals.html", staff=g.staff_browser, business=g.staff_browser.business,
            enabled=False, invitation_url=None, metrics={"registered": 0, "pending": 0, "verified": 0},
        ), 403
    counts = dict(db.session.query(
        ReferralEligibility.status, func.count(ReferralEligibility.id),
    ).filter_by(
        business_id=g.staff_browser.business_id, participant_id=participant.id,
    ).group_by(ReferralEligibility.status).all())
    metrics = {
        "registered": ReferralAttribution.query.filter_by(
            business_id=g.staff_browser.business_id, participant_id=participant.id,
        ).count(),
        "pending": counts.get("pending", 0),
        "verified": counts.get("verified", 0),
    }
    return render_template(
        "staff/referrals.html", staff=g.staff_browser, business=g.staff_browser.business,
        enabled=True, invitation_url=_invitation_url(participant), metrics=metrics,
    )


@staff_portal_bp.get("/portal/referrals/qr.svg")
@staff_portal_required("refer_customers")
def staff_referral_qr():
    participant = _staff_invitation()
    invitation_url = _invitation_url(participant) if staff_can_issue(participant) else None
    if not invitation_url:
        return jsonify({"status": "error", "message": "Los referidos no están habilitados."}), 403
    widget = QrCodeWidget(invitation_url)
    x1, y1, x2, y2 = widget.getBounds()
    drawing = Drawing(x2 - x1, y2 - y1, transform=[1, 0, 0, 1, -x1, -y1])
    drawing.add(widget)
    output = renderSVG.drawToString(drawing).encode("utf-8")
    return current_app.response_class(output, mimetype="image/svg+xml", headers={
        "Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff",
    })
