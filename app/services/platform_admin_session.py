import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import select, update

from app.extensions import db
from app.models.platform_admin_session import PlatformAdminSession
from app.models.platform_admin import PlatformAdmin


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def issue(admin, *, verified_password_hash=None):
    expected = verified_password_hash if verified_password_hash is not None else admin.password_hash
    current = db.session.execute(select(PlatformAdmin).where(
        PlatformAdmin.id == admin.id,
    ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if current is None or not current.is_active or not secrets.compare_digest(current.password_hash, expected):
        db.session.rollback()
        return None
    token = secrets.token_urlsafe(32)
    lifetime = current_app.permanent_session_lifetime
    db.session.add(PlatformAdminSession(
        token_digest=digest(token), admin_id=admin.id,
        password_digest=digest(expected),
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
