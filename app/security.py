import hmac
import re
import secrets
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from app.extensions import db
from app.models.platform_admin import PlatformAdmin


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def current_platform_admin():
    admin_id = session.get("platform_admin_id")
    if not admin_id:
        return None
    admin = db.session.get(PlatformAdmin, admin_id)
    return admin if admin and admin.is_active else None


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
