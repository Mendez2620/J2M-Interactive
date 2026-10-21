import hashlib
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import current_app, g, redirect, request, session, url_for

from app.extensions import db
from app.models import Staff, StaffSession
from app.permissions import effective_permissions, has_permission


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_browser_session(staff, prefix, remember=False):
    from app.security import csrf_token

    session.clear()
    session_id = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    lifetime = (
        current_app.config["REMEMBER_SESSION_ABSOLUTE_SECONDS"]
        if remember else current_app.config["BROWSER_SESSION_ABSOLUTE_SECONDS"]
    )
    expires = _now() + timedelta(seconds=lifetime)
    db.session.add(StaffSession(
        id=session_id, staff_id=staff.id, channel="browser",
        refresh_jti_hash=hashlib.sha256(verifier.encode()).hexdigest(), expires_at=expires,
    ))
    db.session.commit()
    session.permanent = bool(remember)
    session[f"{prefix}_staff_id"] = staff.id
    session[f"{prefix}_session_id"] = session_id
    session[f"{prefix}_auth_version"] = staff.auth_version
    session[f"{prefix}_remember"] = bool(remember)
    session[f"{prefix}_last_active_at"] = time.time()
    # Rotate CSRF together with the authenticated browser session.  Do not wait
    # for a later template render: AJAX and logout must share this exact token.
    csrf_token()


def create_manager_browser_session(staff, remember=False):
    _create_browser_session(staff, "manager", remember=remember)


def create_staff_browser_session(staff, remember=False):
    _create_browser_session(staff, "staff", remember=remember)


def revoke_current_manager_session():
    _revoke_browser_session("manager")


def revoke_current_staff_session():
    _revoke_browser_session("staff")


def _revoke_browser_session(prefix):
    session_id = session.get(f"{prefix}_session_id")
    auth_session = db.session.get(StaffSession, session_id) if session_id else None
    if auth_session and not auth_session.revoked_at:
        auth_session.revoked_at = _now()
        db.session.commit()
    for key in (
        f"{prefix}_staff_id", f"{prefix}_session_id", f"{prefix}_auth_version",
        f"{prefix}_remember", f"{prefix}_last_active_at",
    ):
        session.pop(key, None)


def current_manager():
    return _current_browser_staff("manager", "manager")


def current_staff_browser_session():
    return _current_browser_staff("staff", "cashier")


def _current_browser_staff(prefix, required_role):
    staff_id = session.get(f"{prefix}_staff_id")
    session_id = session.get(f"{prefix}_session_id")
    auth_version = session.get(f"{prefix}_auth_version")
    if not staff_id or not session_id:
        return None
    staff = db.session.get(Staff, staff_id)
    auth_session = db.session.get(StaffSession, session_id)
    if (
        not staff or not auth_session or auth_session.staff_id != staff.id
        or auth_session.channel != "browser" or auth_session.is_revoked
        or auth_session.expires_at <= _now() or auth_version != staff.auth_version
        or staff.role != required_role or not staff.is_active or not staff.business
        or not staff.business.is_active or not staff.web_access_enabled
        or not staff.business.web_access_enabled
    ):
        return None
    last_active_at = session.get(f"{prefix}_last_active_at")
    if session.get(f"{prefix}_remember"):
        idle_limit = current_app.config["REMEMBER_SESSION_IDLE_SECONDS"]
        if not isinstance(last_active_at, (int, float)) or time.time() - last_active_at > idle_limit:
            return None
    session[f"{prefix}_last_active_at"] = time.time()
    return staff


def manager_portal_required(permission=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            staff = current_manager()
            if not staff:
                revoke_current_manager_session()
                return redirect(url_for("manager.manager_login", next=request.path))
            if permission and not has_permission(staff, permission):
                return redirect(url_for("manager.manager_account", denied=permission))
            g.manager = staff
            g.manager_permissions = effective_permissions(staff)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def staff_portal_required(permission=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            staff = current_staff_browser_session()
            if not staff:
                revoke_current_staff_session()
                return redirect(url_for("staff_portal.staff_login", next=request.path))
            if permission and not has_permission(staff, permission):
                return redirect(url_for("staff_portal.staff_login", denied=permission))
            g.staff_browser = staff
            g.staff_browser_permissions = effective_permissions(staff)
            return view(*args, **kwargs)
        return wrapped
    return decorator
