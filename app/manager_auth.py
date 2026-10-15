import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import current_app, g, redirect, request, session, url_for

from app.extensions import db
from app.models import Staff, StaffSession
from app.permissions import effective_permissions, has_permission


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_manager_browser_session(staff):
    session.clear()
    session_id = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    expires = _now() + timedelta(seconds=current_app.config["PERMANENT_SESSION_LIFETIME"])
    db.session.add(StaffSession(
        id=session_id, staff_id=staff.id, channel="browser",
        refresh_jti_hash=hashlib.sha256(verifier.encode()).hexdigest(), expires_at=expires,
    ))
    db.session.commit()
    session.permanent = True
    session["manager_staff_id"] = staff.id
    session["manager_session_id"] = session_id
    session["manager_auth_version"] = staff.auth_version


def revoke_current_manager_session():
    session_id = session.get("manager_session_id")
    auth_session = db.session.get(StaffSession, session_id) if session_id else None
    if auth_session and not auth_session.revoked_at:
        auth_session.revoked_at = _now()
        db.session.commit()
    for key in ("manager_staff_id", "manager_session_id", "manager_auth_version"):
        session.pop(key, None)


def current_manager():
    staff_id = session.get("manager_staff_id")
    session_id = session.get("manager_session_id")
    auth_version = session.get("manager_auth_version")
    if not staff_id or not session_id:
        return None
    staff = db.session.get(Staff, staff_id)
    auth_session = db.session.get(StaffSession, session_id)
    if (
        not staff or not auth_session or auth_session.staff_id != staff.id
        or auth_session.channel != "browser" or auth_session.is_revoked
        or auth_session.expires_at <= _now() or auth_version != staff.auth_version
        or staff.role != "manager" or not staff.is_active or not staff.business
        or not staff.business.is_active or not staff.web_access_enabled
        or not staff.business.web_access_enabled
    ):
        return None
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
