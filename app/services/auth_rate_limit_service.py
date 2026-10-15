import hashlib
import hmac
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import AuthRateLimitSlot


class AuthRateLimitUnavailable(Exception):
    pass


def _hash(value, purpose):
    secret = current_app.config["SECRET_KEY"].encode("utf-8")
    return hmac.new(secret, f"auth-rate:{purpose}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _claim(purpose, dimension, key_hash, limit, now, reserved_until):
    table = AuthRateLimitSlot.__table__
    dialect = db.session.get_bind().dialect.name
    insert_factory = sqlite_insert if dialect == "sqlite" else postgresql_insert
    for slot in range(limit):
        statement = insert_factory(table).values(
            purpose=purpose, dimension=dimension, key_hash=key_hash, slot=slot,
            reserved_until=reserved_until, updated_at=now,
        ).on_conflict_do_update(
            index_elements=["purpose", "dimension", "key_hash", "slot"],
            set_={"reserved_until": reserved_until, "updated_at": now},
            where=table.c.reserved_until <= now,
        )
        if db.session.execute(statement).rowcount == 1:
            return slot
    return None


def reserve_auth_attempt(purpose, identity, client_ip):
    """Atomically reserve both identity and IP quota in one transaction."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    window = current_app.config["AUTH_RATE_LIMIT_WINDOW"]
    expires = now + timedelta(seconds=window)
    limit = current_app.config["AUTH_RATE_LIMIT_ATTEMPTS"]
    identity_hash = _hash(identity.strip().lower() or "missing", purpose)
    ip_hash = _hash(client_ip or "unknown", purpose)
    try:
        identity_slot = _claim(purpose, "identity", identity_hash, limit, now, expires)
        ip_slot = _claim(purpose, "ip", ip_hash, limit, now, expires) if identity_slot is not None else None
        if identity_slot is None or ip_slot is None:
            db.session.rollback()
            return None
        db.session.execute(delete(AuthRateLimitSlot).where(AuthRateLimitSlot.reserved_until < now - timedelta(seconds=window)))
        db.session.commit()
        return identity_hash, identity_slot, ip_hash, ip_slot
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise AuthRateLimitUnavailable from exc


def clear_auth_attempts(purpose, reservation):
    if not reservation:
        return
    identity_hash, identity_slot, ip_hash, ip_slot = reservation
    try:
        db.session.execute(delete(AuthRateLimitSlot).where(
            AuthRateLimitSlot.purpose == purpose,
            ((AuthRateLimitSlot.dimension == "identity") & (AuthRateLimitSlot.key_hash == identity_hash) & (AuthRateLimitSlot.slot == identity_slot))
            | ((AuthRateLimitSlot.dimension == "ip") & (AuthRateLimitSlot.key_hash == ip_hash) & (AuthRateLimitSlot.slot == ip_slot)),
        ))
        db.session.commit()
    except SQLAlchemyError as exc:
        db.session.rollback()
        raise AuthRateLimitUnavailable from exc
