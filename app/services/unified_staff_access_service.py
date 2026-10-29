from datetime import datetime, timedelta, timezone

from flask import current_app, redirect, render_template, request, session, url_for
from sqlalchemy import func
from sqlalchemy.orm import joinedload
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.manager_auth import create_manager_browser_session, create_staff_browser_session
from app.models import Business, Staff
from app.permissions import has_permission
from app.security import valid_email
from app.services.auth_rate_limit_service import (
    AuthRateLimitUnavailable, clear_auth_attempts, reserve_auth_attempt,
)


_DUMMY_PASSWORD_HASH = generate_password_hash("J2M-invalid-credential-padding-only")
_GENERIC_ERROR = "No fue posible iniciar sesión con esas credenciales."


def _now_timestamp():
    return datetime.now(timezone.utc).timestamp()


def _eligible(staff):
    if not (
        staff and staff.business and staff.role in {"manager", "cashier"}
        and staff.is_active and staff.business.is_active
        and staff.web_access_enabled and staff.business.web_access_enabled
    ):
        return False
    return staff.role == "manager" or has_permission(staff, "scanner_operate_web")


def _destination(staff):
    if staff.role == "cashier":
        return url_for("staff_portal.staff_scanner")
    from app.routes.manager import _portal_destination
    return _portal_destination(staff)


def _start_session(staff, remember):
    if staff.role == "manager":
        create_manager_browser_session(staff, remember=remember)
    else:
        create_staff_browser_session(staff, remember=remember)
    return redirect(_destination(staff))


def _pending_accounts():
    pending = session.get("staff_access_choice")
    if not isinstance(pending, dict) or pending.get("expires_at", 0) < _now_timestamp():
        session.pop("staff_access_choice", None)
        return None, []
    records = []
    for item in pending.get("accounts", []):
        if not isinstance(item, dict):
            continue
        staff = db.session.get(Staff, item.get("staff_id"))
        if (
            staff and staff.auth_version == item.get("auth_version")
            and staff.business_id == item.get("business_id") and staff.role == item.get("role")
            and _eligible(staff)
        ):
            records.append(staff)
    if not records:
        session.pop("staff_access_choice", None)
        return None, []
    return pending, records


def _select_pending_account(account_id):
    pending, records = _pending_accounts()
    if not pending:
        return None
    allowed = {staff.id: staff for staff in records}
    staff = allowed.get(account_id)
    if not staff:
        session.pop("staff_access_choice", None)
        return None
    session.pop("staff_access_choice", None)
    return _start_session(staff, bool(pending.get("remember")))


def bounded_password_matches(identity, password, *, allowed_roles=None, business_slug=None, allow_username=False):
    """Authenticate against a bounded candidate set with a fixed hash budget."""
    maximum = max(1, min(current_app.config["STAFF_ACCESS_MAX_EMAIL_CANDIDATES"], 20))
    query = Staff.query.options(joinedload(Staff.business)).join(Business)
    if identity and password and valid_email(identity):
        query = query.filter(func.lower(Staff.email) == identity)
    elif identity and password and allow_username and business_slug:
        query = query.filter(Staff.username == identity)
    else:
        query = None
    if query is not None and business_slug:
        query = query.filter(Business.slug == business_slug)
    if query is not None and allowed_roles:
        query = query.filter(Staff.role.in_(allowed_roles))
    candidates = query.order_by(Staff.id).limit(maximum + 1).all() if query is not None else []
    overflow = len(candidates) > maximum
    candidates = candidates[:maximum]
    matches = []
    for index in range(maximum):
        candidate = candidates[index] if index < len(candidates) else None
        password_hash = candidate.password_hash if candidate else _DUMMY_PASSWORD_HASH
        try:
            matched = check_password_hash(password_hash, password)
        except (TypeError, ValueError):
            matched = False
        if (
            candidate and matched and _eligible(candidate)
            and (not allowed_roles or candidate.role in allowed_roles)
        ):
            matches.append(candidate)
    return [] if overflow else matches


def unified_staff_access():
    error = "La sesión de seguridad cambió. Inicia sesión nuevamente." if request.args.get("csrf_error") else None
    status = 200
    pending, account_choices = _pending_accounts()
    if request.method == "POST":
        account_id = request.form.get("account_id", type=int)
        if account_id is not None:
            response = _select_pending_account(account_id)
            if response:
                return response
            error, status, account_choices = _GENERIC_ERROR, 401, []
        else:
            session.pop("staff_access_choice", None)
            session.pop("manager_account_choice", None)
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            remember = request.form.get("remember") == "1"
            try:
                reservation = reserve_auth_attempt("staff-access", email, request.remote_addr)
            except AuthRateLimitUnavailable:
                current_app.logger.exception("auth_rate_limit_unavailable component=staff_access")
                reservation = None
                error, status = "Inicio de sesión temporalmente no disponible.", 503
            if status == 200 and reservation is None:
                error, status = "Demasiados intentos. Intenta más tarde.", 429
            elif status == 200:
                matches = bounded_password_matches(email, password)
                if not matches:
                    error, status = _GENERIC_ERROR, 401
                else:
                    clear_auth_attempts("staff-access", reservation)
                    if len(matches) == 1:
                        return _start_session(matches[0], remember)
                    session["staff_access_choice"] = {
                        "accounts": [{
                            "staff_id": staff.id,
                            "business_id": staff.business_id,
                            "role": staff.role,
                            "auth_version": staff.auth_version,
                        } for staff in matches],
                        "expires_at": (
                            datetime.now(timezone.utc)
                            + timedelta(seconds=current_app.config["STAFF_ACCESS_CHOICE_SECONDS"])
                        ).timestamp(),
                        "remember": remember,
                    }
                    account_choices = matches
    return render_template(
        "staff/access.html", error=error, account_choices=account_choices,
        account_form_action=url_for("main.staff_access"),
    ), status
