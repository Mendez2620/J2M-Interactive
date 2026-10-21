import json
import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, or_, text

from app.extensions import db
from app.models import Customer, GoogleWalletSyncJob
from app.services.google_wallet import GoogleWalletService


_SQLITE_LOCKS = {}
_SQLITE_LOCKS_GUARD = threading.Lock()
_ADVISORY_NAMESPACE = 1246907724


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _eligible(now):
    stale = now - timedelta(minutes=5)
    return (
        or_(
            GoogleWalletSyncJob.status == "pending",
            (GoogleWalletSyncJob.status == "processing") &
            or_(GoogleWalletSyncJob.locked_at.is_(None),
                GoogleWalletSyncJob.locked_at < stale),
        ),
        or_(
            GoogleWalletSyncJob.next_attempt_at.is_(None),
            GoogleWalletSyncJob.next_attempt_at <= now,
        ),
    )


@contextmanager
def _customer_lock(customer_id):
    """Fence Google updates per customer without blocking loyalty transactions."""
    dialect = db.session.get_bind().dialect.name
    local_lock = None
    if dialect == "postgresql":
        acquired = bool(db.session.execute(
            text("SELECT pg_try_advisory_xact_lock(:namespace, :customer_id)"),
            {"namespace": _ADVISORY_NAMESPACE, "customer_id": customer_id},
        ).scalar())
    else:
        with _SQLITE_LOCKS_GUARD:
            local_lock = _SQLITE_LOCKS.setdefault(customer_id, threading.Lock())
        acquired = local_lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if local_lock is not None and acquired:
            local_lock.release()


def _record_result(job_ids, result, now):
    status = result.get("status")
    values = {"locked_at": None, "last_status_code": result.get("status_code")}
    if status == "success":
        values.update(status="completed", last_status_code=None, next_attempt_at=None)
    elif status == "error":
        values.update(status="failed", attempts=GoogleWalletSyncJob.attempts + 1,
                      next_attempt_at=None)
    else:
        max_attempts = db.session.query(func.max(GoogleWalletSyncJob.attempts)).filter(
            GoogleWalletSyncJob.id.in_(job_ids)
        ).scalar() or 0
        delay = min(3600, 30 * (2 ** min(max_attempts + 1, 6)))
        values.update(status="pending", attempts=GoogleWalletSyncJob.attempts + 1,
                      next_attempt_at=now + timedelta(seconds=delay))
    GoogleWalletSyncJob.query.filter(GoogleWalletSyncJob.id.in_(job_ids)).update(
        values, synchronize_session=False
    )


def process_pending_google_wallet_jobs(limit=50, service=None):
    """Process current balances with a per-customer fence and job watermark."""
    service = service or GoogleWalletService()
    if getattr(service, "mode", None) == "disabled":
        return 0
    now = _now()
    customer_ids = [row[0] for row in db.session.query(GoogleWalletSyncJob.customer_id).filter(
        *_eligible(now)
    ).group_by(GoogleWalletSyncJob.customer_id).order_by(
        func.min(GoogleWalletSyncJob.id)
    ).limit(limit).all()]
    db.session.rollback()

    processed = 0
    for customer_id in customer_ids:
        now = _now()
        try:
            with _customer_lock(customer_id) as acquired:
                if not acquired:
                    db.session.rollback()
                    continue
                jobs = GoogleWalletSyncJob.query.filter(
                    GoogleWalletSyncJob.customer_id == customer_id, *_eligible(now)
                ).order_by(GoogleWalletSyncJob.id).limit(max(1, limit - processed)).all()
                if not jobs:
                    db.session.rollback()
                    continue
                job_ids = [job.id for job in jobs]
                watermark = max(job_ids)
                GoogleWalletSyncJob.query.filter(
                    GoogleWalletSyncJob.id.in_(job_ids),
                    GoogleWalletSyncJob.id <= watermark,
                ).update({"status": "processing", "locked_at": now}, synchronize_session=False)
                customer = db.session.get(Customer, customer_id)
                if customer is None:
                    _record_result(job_ids, {"status": "error", "status_code": None}, now)
                else:
                    try:
                        result = service.update_loyalty_object(customer)
                    except Exception:
                        result = {"status": "retry", "status_code": None}
                    _record_result(job_ids, result, now)
                db.session.commit()
                processed += len(job_ids)
        except Exception:
            db.session.rollback()
            raise
        if processed >= limit:
            break
    return processed


def google_wallet_sync_counts():
    counts = dict(db.session.query(
        GoogleWalletSyncJob.status, func.count(GoogleWalletSyncJob.id)
    ).group_by(GoogleWalletSyncJob.status).all())
    return {
        "pending": counts.get("pending", 0) + counts.get("processing", 0),
        "failed": counts.get("failed", 0),
        "completed": counts.get("completed", 0),
    }


def google_wallet_sync_metrics():
    """Return safe aggregate queue health without customer or credential data."""
    now = _now()
    rows = dict(db.session.query(
        GoogleWalletSyncJob.status, func.count(GoogleWalletSyncJob.id)
    ).group_by(GoogleWalletSyncJob.status).all())
    oldest_pending = db.session.query(func.min(GoogleWalletSyncJob.created_at)).filter(
        GoogleWalletSyncJob.status.in_(("pending", "processing"))
    ).scalar()
    last_attempt = db.session.query(func.max(GoogleWalletSyncJob.updated_at)).filter(
        or_(GoogleWalletSyncJob.attempts > 0, GoogleWalletSyncJob.status != "pending")
    ).scalar()
    last_success = db.session.query(func.max(GoogleWalletSyncJob.updated_at)).filter_by(
        status="completed"
    ).scalar()
    pending = rows.get("pending", 0)
    processing = rows.get("processing", 0)
    retrying = db.session.query(func.count(GoogleWalletSyncJob.id)).filter(
        GoogleWalletSyncJob.status == "pending", GoogleWalletSyncJob.attempts > 0
    ).scalar() or 0
    return {
        "pending": pending, "processing": processing, "retrying": retrying,
        "completed": rows.get("completed", 0), "failed": rows.get("failed", 0),
        "oldest_pending_seconds": int((now - oldest_pending).total_seconds()) if oldest_pending else None,
        "last_attempt_at": last_attempt.isoformat() if last_attempt else None,
        "last_success_at": last_success.isoformat() if last_success else None,
    }


def google_wallet_sync_business_summaries(business_ids=None):
    """Aggregate queue state in one grouped query for the Super Admin table."""
    query = db.session.query(
        GoogleWalletSyncJob.business_id, GoogleWalletSyncJob.status,
        func.count(GoogleWalletSyncJob.id), func.max(GoogleWalletSyncJob.updated_at),
        func.sum(case((GoogleWalletSyncJob.attempts > 0, 1), else_=0)),
    )
    if business_ids is not None:
        query = query.filter(GoogleWalletSyncJob.business_id.in_(business_ids))
    summaries = {}
    for business_id, status, count, updated_at, attempted in query.group_by(
        GoogleWalletSyncJob.business_id, GoogleWalletSyncJob.status
    ).all():
        summary = summaries.setdefault(business_id, {
            "pending": 0, "processing": 0, "retrying": 0, "completed": 0,
            "failed": 0, "last_attempt_at": None, "last_success_at": None,
        })
        summary[status] = count
        if (attempted or status != "pending") and (
            summary["last_attempt_at"] is None or updated_at > summary["last_attempt_at"]
        ):
            summary["last_attempt_at"] = updated_at
        if status == "pending":
            summary["retrying"] += attempted or 0
        if status == "completed" and (summary["last_success_at"] is None or updated_at > summary["last_success_at"]):
            summary["last_success_at"] = updated_at
    return summaries


def run_google_wallet_sync_worker(*, stop_event=None, sleep_fn=time.sleep, service=None,
                                  poll_seconds=10, batch_size=50, alert_pending=100,
                                  alert_stale_seconds=300, max_cycles=None, logger=None):
    """Continuously drain the outbox; per-customer locks make replicas safe."""
    stop_event = stop_event or threading.Event()
    logger = logger or logging.getLogger(__name__)
    cycles = 0
    while not stop_event.is_set():
        try:
            processed = process_pending_google_wallet_jobs(limit=batch_size, service=service)
            metrics = google_wallet_sync_metrics()
            logger.info("google_wallet_worker_metrics %s", json.dumps({"processed": processed, **metrics}, sort_keys=True))
            backlog = metrics["pending"] + metrics["processing"]
            stale = metrics["oldest_pending_seconds"] or 0
            if backlog >= alert_pending or (backlog and stale >= alert_stale_seconds):
                logger.warning(
                    "google_wallet_worker_backlog pending=%s processing=%s retrying=%s oldest_pending_seconds=%s",
                    metrics["pending"], metrics["processing"], metrics["retrying"], stale,
                )
        except Exception as exc:
            db.session.rollback()
            logger.error("google_wallet_worker_cycle_failed error_type=%s", type(exc).__name__)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        if sleep_fn is time.sleep:
            stop_event.wait(max(1, poll_seconds))
        else:
            sleep_fn(max(1, poll_seconds))
