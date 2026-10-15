import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import current_app, g, jsonify, request

from app.extensions import db
from app.models import Staff, StaffSession
from app.permissions import has_permission


def _now():
    return datetime.now(timezone.utc)


def _hash_jti(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _encode(staff, session_id, token_type, lifetime, channel):
    now = _now()
    jti = secrets.token_urlsafe(24)
    payload = {
        "iss": current_app.config["JWT_ISSUER"],
        "aud": current_app.config["JWT_AUDIENCE"],
        "sub": str(staff.id),
        "sid": session_id,
        "jti": jti,
        "typ": token_type,
        "ver": staff.auth_version,
        "chn": channel,
        "iat": now,
        "nbf": now,
        "exp": now + lifetime,
    }
    token = jwt.encode(payload, current_app.config["JWT_SECRET_KEY"], algorithm="HS256")
    return token, jti, payload["exp"]


def issue_token_pair(staff, channel="mobile"):
    if channel not in {"web", "mobile"}:
        raise ValueError("invalid channel")
    session_id = secrets.token_urlsafe(24)
    access, _, _ = _encode(
        staff, session_id, "access", timedelta(seconds=current_app.config["JWT_ACCESS_TOKEN_EXPIRES"]), channel
    )
    refresh, refresh_jti, refresh_exp = _encode(
        staff, session_id, "refresh", timedelta(seconds=current_app.config["JWT_REFRESH_TOKEN_EXPIRES"]), channel
    )
    db.session.add(StaffSession(
        id=session_id,
        staff_id=staff.id,
        channel=channel,
        refresh_jti_hash=_hash_jti(refresh_jti),
        expires_at=refresh_exp.replace(tzinfo=None),
    ))
    db.session.commit()
    return access, refresh


def decode_token(token, expected_type):
    return jwt.decode(
        token,
        current_app.config["JWT_SECRET_KEY"],
        algorithms=["HS256"],
        issuer=current_app.config["JWT_ISSUER"],
        audience=current_app.config["JWT_AUDIENCE"],
        options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub", "sid", "jti", "typ", "ver", "chn"]},
    ) if expected_type is None else _decode_typed(token, expected_type)


def _decode_typed(token, expected_type):
    payload = jwt.decode(
        token,
        current_app.config["JWT_SECRET_KEY"],
        algorithms=["HS256"], issuer=current_app.config["JWT_ISSUER"],
        audience=current_app.config["JWT_AUDIENCE"],
        options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub", "sid", "jti", "typ", "ver", "chn"]},
    )
    if payload["typ"] != expected_type:
        raise jwt.InvalidTokenError("token type mismatch")
    return payload


def revoke_staff_sessions(staff):
    staff.auth_version += 1
    now = _now().replace(tzinfo=None)
    StaffSession.query.filter_by(staff_id=staff.id, revoked_at=None).update({"revoked_at": now})


def _bearer_token():
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.startswith("Bearer ") else None


def authenticate_request(channel, permission=None):
    token = _bearer_token()
    if not token:
        return None, (jsonify({"status": "error", "message": "Autenticación requerida."}), 401)
    try:
        payload = decode_token(token, "access")
        staff = db.session.get(Staff, int(payload["sub"]))
        auth_session = db.session.get(StaffSession, payload["sid"])
    except (jwt.PyJWTError, TypeError, ValueError):
        return None, (jsonify({"status": "error", "message": "Token inválido o expirado."}), 401)
    if not staff or not auth_session or auth_session.is_revoked or payload["ver"] != staff.auth_version or payload["chn"] != channel or auth_session.channel != channel:
        return None, (jsonify({"status": "error", "message": "Sesión revocada o inválida."}), 401)
    if not staff.is_active or not staff.business or not staff.business.is_active:
        return None, (jsonify({"status": "error", "message": "Cuenta o negocio inactivo."}), 403)
    if channel not in {"web", "mobile"}:
        return None, (jsonify({"status": "error", "message": "Canal inválido."}), 403)
    if not getattr(staff.business, f"{channel}_access_enabled") or not getattr(staff, f"{channel}_access_enabled"):
        return None, (jsonify({"status": "error", "message": f"Acceso {channel} deshabilitado."}), 403)
    if permission and not has_permission(staff, permission):
        return None, (jsonify({"status": "error", "message": "Permiso insuficiente."}), 403)
    return staff, None


def staff_required(channel, permission=None, roles=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            staff, error = authenticate_request(channel, permission)
            if error:
                return error
            if roles and staff.role not in roles:
                return jsonify({"status": "error", "message": "Rol no autorizado."}), 403
            g.staff = staff
            return view(*args, **kwargs)
        return wrapped
    return decorator


def staff_any_channel_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        try:
            payload = decode_token(_bearer_token(), "access")
            channel = payload["chn"]
        except (jwt.PyJWTError, TypeError, ValueError, KeyError):
            return jsonify({"status": "error", "message": "Token inválido o expirado."}), 401
        if channel not in {"web", "mobile"}:
            return jsonify({"status": "error", "message": "Canal inválido."}), 401
        staff, error = authenticate_request(channel)
        if error:
            return error
        g.staff = staff
        return view(*args, **kwargs)
    return wrapped
