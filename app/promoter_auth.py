import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import current_app, g, redirect, request, session, url_for

from app.extensions import db
from app.models import Promoter, PromoterSession


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_promoter_session(promoter):
    from app.security import csrf_token
    session.clear()
    session_id, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.session.add(PromoterSession(
        id=session_id, promoter_id=promoter.id,
        verifier_hash=hashlib.sha256(verifier.encode()).hexdigest(),
        expires_at=_now() + timedelta(seconds=current_app.config["BROWSER_SESSION_ABSOLUTE_SECONDS"]),
    ))
    db.session.commit()
    session.permanent = False
    session["promoter_id"] = promoter.id
    session["promoter_session_id"] = session_id
    session["promoter_session_verifier"] = verifier
    session["promoter_auth_version"] = promoter.auth_version
    session["promoter_last_active_at"] = time.time()
    csrf_token()


def current_promoter():
    promoter_id, session_id = session.get("promoter_id"), session.get("promoter_session_id")
    verifier = session.get("promoter_session_verifier")
    if not promoter_id or not session_id or not isinstance(verifier, str):
        return None
    promoter, auth_session = db.session.get(Promoter, promoter_id), db.session.get(PromoterSession, session_id)
    verifier_hash = hashlib.sha256(verifier.encode()).hexdigest()
    if (not promoter or not auth_session or auth_session.promoter_id != promoter.id
            or not hmac.compare_digest(auth_session.verifier_hash, verifier_hash)
            or auth_session.revoked_at or auth_session.expires_at <= _now()
            or promoter.auth_version != session.get("promoter_auth_version")
            or not promoter.is_active or not promoter.business or not promoter.business.is_active):
        return None
    session["promoter_last_active_at"] = time.time()
    return promoter


def revoke_promoter_sessions(promoter):
    now = _now()
    PromoterSession.query.filter_by(promoter_id=promoter.id, revoked_at=None).update({"revoked_at": now})
    promoter.auth_version += 1


def revoke_current_promoter_session():
    session_id = session.get("promoter_session_id")
    auth_session = db.session.get(PromoterSession, session_id) if session_id else None
    if auth_session and not auth_session.revoked_at:
        auth_session.revoked_at = _now()
        db.session.commit()
    for key in ("promoter_id", "promoter_session_id", "promoter_session_verifier",
                "promoter_auth_version", "promoter_last_active_at"):
        session.pop(key, None)


def promoter_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        promoter = current_promoter()
        if not promoter:
            revoke_current_promoter_session()
            return redirect(url_for("promoter.promoter_login", next=request.path))
        g.promoter = promoter
        return view(*args, **kwargs)
    return wrapped
