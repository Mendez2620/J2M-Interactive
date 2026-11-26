import hmac
import re
import secrets
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from app.extensions import db
from app.models.platform_admin import PlatformAdmin
from app.models.platform_admin_session import PlatformAdminSession
from app.services.platform_admin_session import digest, now
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def current_platform_admin():
    admin_id = session.get("platform_admin_id")
    token = session.get("platform_admin_session")
    if type(admin_id) is not int or not isinstance(token, str) or len(token) != 43:
        return None
    try:
        row = db.session.execute(
            select(PlatformAdmin, PlatformAdminSession.password_digest)
            .join(PlatformAdminSession, PlatformAdminSession.admin_id == PlatformAdmin.id)
            .where(PlatformAdmin.id == admin_id, PlatformAdmin.is_active.is_(True),
                   PlatformAdminSession.token_digest == digest(token),
                   PlatformAdminSession.revoked_at.is_(None), PlatformAdminSession.expires_at > now())
            .execution_options(populate_existing=True)
        ).one_or_none()
    except SQLAlchemyError:
        db.session.rollback()
        return None
    if row and hmac.compare_digest(digest(row[0].password_hash), row[1]):
        return row[0]
    return None


def wants_json():
    return request.is_json or request.accept_mimetypes.best == "application/json"


def platform_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_platform_admin():
            if wants_json():
                return jsonify({"status": "error", "message": "Autenticación requerida."}), 401
            return redirect(url_for("auth.admin_login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def validate_csrf():
    expected = session.get("csrf_token")
    supplied = request.headers.get("X-CSRFToken") or request.form.get("csrf_token")
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def validate_password(password):
    if len(password or "") < 12:
        return "La contraseña debe tener al menos 12 caracteres."
    if not re.search(r"[A-Z]", password) or not re.search(r"[a-z]", password):
        return "La contraseña debe incluir mayúsculas y minúsculas."
    if not re.search(r"\d", password):
        return "La contraseña debe incluir al menos un número."
    return None


def valid_email(email):
    return bool(EMAIL_RE.fullmatch(email or ""))
