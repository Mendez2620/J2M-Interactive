"""Persistent handoff from a reviewed selection to the Wallet worker."""

from dataclasses import asdict
from json import dumps
from uuid import UUID, uuid4

from flask import current_app
from sqlalchemy import select, update

from app.extensions import db
from app.models import Business, PlatformAdmin
from app.models.platform_admin_session import AdminWalletReview, PlatformAdminSession
from app.services.google_wallet_reconciliation_plan import preview_google_wallet_reconciliation
from app.services.platform_admin_session import digest, now


def enabled():
    return (current_app.config.get("GOOGLE_WALLET_RECONCILIATION_ENABLED") is True
            and current_app.config.get("GOOGLE_WALLET_MODE") == "real")


def fingerprint(candidates):
    return digest(dumps([asdict(item) for item in candidates], sort_keys=True, default=str))


def revalidate(review):
    preview = preview_google_wallet_reconciliation(
        review.business_id, limit=25, customer_ids=review.candidate_ids,
    )
    if preview.business_active and fingerprint(preview.candidates) == review.fingerprint:
        return preview.candidates
    return ()


def confirm(review):
    if review.state != "prepared":
        return
    state = "expired" if review.expires_at <= now() else "queued" if revalidate(review) else "rejected"
    db.session.execute(update(AdminWalletReview).where(
        AdminWalletReview.id == review.id, AdminWalletReview.state == "prepared",
        AdminWalletReview.session_digest == review.session_digest,
        AdminWalletReview.business_id == review.business_id,
    ).values(state=state, confirmed_at=now() if state == "queued" else None))
    db.session.commit()


def _authorized_admin(session_digest, business_id, expires_at):
    if not enabled() or expires_at <= now():
        return None
    with db.engine.connect() as connection:
        row = connection.execute(select(
            PlatformAdmin.id, PlatformAdmin.password_hash, PlatformAdminSession.password_digest,
        ).join(PlatformAdminSession, PlatformAdminSession.admin_id == PlatformAdmin.id)
          .join(Business, Business.id == business_id)
          .where(PlatformAdminSession.token_digest == session_digest,
                 PlatformAdminSession.revoked_at.is_(None),
                 PlatformAdminSession.expires_at > now(), PlatformAdmin.is_active.is_(True),
                 Business.is_active.is_(True))).one_or_none()
    return row[0] if row and digest(row[1]) == row[2] else None


def process_one_admin_wallet_review(service=None):
    if not enabled():
        return 0
    model = AdminWalletReview
    owner = uuid4().hex
    review_id = db.session.scalar(select(model.id).where(model.state == "queued")
                                  .order_by(model.created_at, model.id).limit(1))
    if review_id is None:
        db.session.rollback()
        return 0
    changed = db.session.execute(update(model).where(
        model.id == review_id, model.state == "queued",
    ).values(state="running", owner=owner, claimed_at=now()))
    claimed = changed.rowcount == 1
    db.session.commit()
    if not claimed:
        return 0

    review = db.session.get(model, review_id)
    session_digest, business_id, expires_at = review.session_digest, review.business_id, review.expires_at
    state, result = "inconclusive", None
    try:
        admin_id = _authorized_admin(session_digest, business_id, expires_at)
        candidates = revalidate(review) if admin_id else ()
        db.session.rollback()
        if not candidates:
            state = "expired" if expires_at <= now() else "rejected"
        elif _authorized_admin(session_digest, business_id, expires_at) == admin_id:
            from app.services.google_wallet import GoogleWalletService
            from app.services.google_wallet_reconciliation_control import run_reviewed_reconciliation

            result = run_reviewed_reconciliation(
                candidates, business_id=business_id, admin_id=admin_id,
                request_id=str(UUID(hex=review_id)), confirm=True,
                service=service if service is not None else GoogleWalletService(),
                authorize=lambda: _authorized_admin(session_digest, business_id, expires_at) == admin_id,
            )
            if result.get("status") == "finished":
                state = "finished"
    except Exception:
        db.session.rollback()
        current_app.logger.warning("admin_wallet_review_execution_unconfirmed")
    db.session.execute(update(model).where(
        model.id == review_id, model.state == "running", model.owner == owner,
    ).values(state=state, result=result, finished_at=now()))
    db.session.commit()
    return 1
