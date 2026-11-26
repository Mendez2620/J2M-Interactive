import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import update

from app.extensions import db
from app.models.platform_admin_session import PlatformAdminSession


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def issue(admin):
    token = secrets.token_urlsafe(32)
    lifetime = current_app.permanent_session_lifetime
    db.session.add(PlatformAdminSession(
        token_digest=digest(token), admin_id=admin.id,
        password_digest=digest(admin.password_hash),
        expires_at=now() + min(lifetime, timedelta(hours=1)),
    ))
    db.session.commit()
    return token


def revoke(admin_id, token):
    if not isinstance(token, str) or len(token) != 43:
        return
    db.session.execute(update(PlatformAdminSession).where(
        PlatformAdminSession.admin_id == admin_id,
        PlatformAdminSession.token_digest == digest(token),
        PlatformAdminSession.revoked_at.is_(None),
    ).values(revoked_at=now()))
    db.session.commit()
