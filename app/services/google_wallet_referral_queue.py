"""Durable, bounded publication of referral metadata for existing cards."""

from datetime import timedelta
from uuid import uuid4

from flask import current_app
from sqlalchemy import and_, func, or_, select, update

from app.extensions import db
from app.models import Customer, GoogleWalletReferralJob as Job, ReferralParticipant
from app.models.google_wallet_referral_job import utcnow
from app.services.google_wallet import GoogleWalletService
from app.services.google_wallet_write_reservation import authorize_wallet_writes


def process_wallet_referral_jobs(*, service=None, limit=10):
    if current_app.config.get("GOOGLE_WALLET_MODE") != "real":
        return 0
    if service is not None and service.mode != "real":
        return 0
    now = utcnow()
    due = and_(Job.run_at <= now, or_(
        Job.status == "pending",
        and_(Job.status == "processing", Job.locked_at < now - timedelta(minutes=5)),
    ))
    ids = db.session.scalars(select(Job.id).where(due).order_by(Job.run_at, Job.id)
                             .limit(min(max(1, limit), 25))).all()
    db.session.rollback()
    processed = 0
    for job_id in ids:
        token = uuid4().hex
        claimed = db.session.execute(update(Job).where(Job.id == job_id, due).values(
            status="processing", token=token, locked_at=now,
        ).returning(Job.id)).scalar_one_or_none()
        db.session.commit()
        if claimed is None:
            continue
        row = db.session.get(Job, job_id)
        if row is None:
            db.session.rollback()
            continue
        business_id, customer_id, cursor, attempts = row.business_id, row.customer_id, row.cursor, row.attempts
        owned = (Job.id == job_id, Job.status == "processing", Job.token == token)
        values = dict(token=None, locked_at=None, status="completed", outcome="confirmed")
        if customer_id is None:
            page = db.session.scalars(select(ReferralParticipant.customer_id).where(
                ReferralParticipant.business_id == business_id,
                ReferralParticipant.customer_id > cursor,
            ).order_by(ReferralParticipant.customer_id).limit(26)).all()
            if len(page) > 25:
                values.update(status="pending", cursor=page[24], outcome=None)
            # Commit the cursor and its page together, or discard both.
            changed = db.session.execute(update(Job).where(*owned).values(**values)).rowcount
            if changed:
                db.session.add_all([Job(business_id=business_id, customer_id=item) for item in page[:25]])
            db.session.commit()
            processed += changed
            continue
        customer = db.session.scalar(select(Customer).where(
            Customer.id == customer_id, Customer.business_id == business_id,
        ))
        db.session.commit()

        def authorize():
            with db.engine.connect() as connection:
                return connection.scalar(select(Job.id).where(*owned)) is not None

        try:
            from app.services.google_wallet_sync_service import _customer_lock

            with authorize_wallet_writes(authorize):
                if customer is None:
                    outcome = {"status": "skipped"}
                else:
                    with _customer_lock(customer_id) as acquired:
                        if not acquired or not authorize():
                            outcome = {"status": "retry"}
                        else:
                            service = service or GoogleWalletService()
                            outcome = service.publish_referral_links(customer)
        except Exception:
            db.session.rollback()
            outcome = {"status": "retry"}
        status = outcome.get("status")
        if status == "skipped":
            values.update(status="missing", outcome="missing")
        elif status == "error":
            values.update(status="failed", outcome="failed", attempts=attempts + 1)
        elif status != "success":
            reason = "blocked" if outcome.get("reason") == "write_reservation_unavailable" else "retry"
            values.update(status="pending", attempts=attempts + 1, outcome=reason,
                          run_at=utcnow() + timedelta(seconds=min(3600, 30 * 2 ** min(attempts + 1, 7))))
        changed = db.session.execute(update(Job).where(*owned).values(**values)).rowcount
        db.session.commit()
        processed += changed
    return processed


def wallet_referral_queue_counts(business_id=None):
    query = select(Job.status, Job.outcome, Job.customer_id.is_(None), Job.scheduled_expiry, func.count(Job.id))
    if business_id is not None:
        query = query.where(Job.business_id == business_id)
    counts = dict.fromkeys(("pending", "processing", "completed", "missing", "failed",
                            "retry", "blocked", "business_pending", "expiry_pending"), 0)
    for status, outcome, business_event, expiry, count in db.session.execute(
        query.group_by(Job.status, Job.outcome, Job.customer_id.is_(None), Job.scheduled_expiry)
    ):
        counts[status] += count
        if status in {"pending", "processing"}:
            counts["business_pending"] += count if business_event else 0
            counts["expiry_pending"] += count if expiry else 0
            if outcome in {"retry", "blocked"}:
                counts[outcome] += count
    return counts
