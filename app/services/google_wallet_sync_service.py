from datetime import datetime, timedelta, timezone

from sqlalchemy import or_

from app.extensions import db
from app.models import GoogleWalletSyncJob
from app.services.google_wallet import GoogleWalletService


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def record_google_wallet_result(transaction_id, result):
    job = GoogleWalletSyncJob.query.filter_by(transaction_id=transaction_id).first()
    if not job:
        return
    if result.get("status") == "success":
        job.status = "completed"
        job.last_status_code = None
    elif result.get("status") == "error":
        job.status = "failed"
        job.last_status_code = result.get("status_code")
        job.attempts += 1
    else:
        job.status = "pending"
        job.last_status_code = result.get("status_code")
        job.attempts += 1
        job.next_attempt_at = _now() + timedelta(seconds=min(3600, 30 * (2 ** min(job.attempts, 6))))
    job.locked_at = None
    db.session.commit()


def process_pending_google_wallet_jobs(limit=50, service=None):
    service = service or GoogleWalletService()
    now = _now()
    stale = now - timedelta(minutes=5)
    jobs = GoogleWalletSyncJob.query.filter(
        or_(
            GoogleWalletSyncJob.status == "pending",
            (GoogleWalletSyncJob.status == "processing") & (GoogleWalletSyncJob.locked_at < stale),
        ),
        or_(GoogleWalletSyncJob.next_attempt_at.is_(None), GoogleWalletSyncJob.next_attempt_at <= now),
    ).order_by(GoogleWalletSyncJob.id).limit(limit).all()
    processed = 0
    for candidate in jobs:
        claimed = GoogleWalletSyncJob.query.filter(
            GoogleWalletSyncJob.id == candidate.id,
            or_(
                GoogleWalletSyncJob.status == "pending",
                (GoogleWalletSyncJob.status == "processing") & (GoogleWalletSyncJob.locked_at < stale),
            ),
        ).update({"status": "processing", "locked_at": now}, synchronize_session=False)
        db.session.commit()
        if claimed != 1:
            continue
        job = db.session.get(GoogleWalletSyncJob, candidate.id)
        try:
            result = service.update_loyalty_object(job.customer)
        except Exception:
            result = {"status": "retry", "status_code": None}
        if result.get("status") == "success":
            GoogleWalletSyncJob.query.filter(
                GoogleWalletSyncJob.customer_id == job.customer_id,
                GoogleWalletSyncJob.status.in_(("pending", "processing")),
            ).update({"status": "completed", "locked_at": None, "last_status_code": None}, synchronize_session=False)
            db.session.commit()
        else:
            record_google_wallet_result(job.transaction_id, result)
        processed += 1
    return processed
