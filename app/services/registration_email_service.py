import base64
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet, InvalidToken
from flask import current_app
from sqlalchemy import or_

from app.email_service import MailDeliveryError
from app.extensions import db
from app.models import Business, CustomerRegistrationChallenge, RegistrationEmailJob


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _cipher():
    digest = hashlib.sha256(current_app.config["REGISTRATION_EMAIL_ENCRYPTION_KEY"].encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_registration_code(code):
    return _cipher().encrypt(code.encode()).decode()


def _eligible(now):
    stale = now - timedelta(seconds=current_app.config["REGISTRATION_EMAIL_STALE_SECONDS"])
    return (
        or_(
            RegistrationEmailJob.status == "pending",
            (RegistrationEmailJob.status == "processing") &
            or_(RegistrationEmailJob.locked_at.is_(None), RegistrationEmailJob.locked_at < stale),
        ),
        or_(RegistrationEmailJob.next_attempt_at.is_(None), RegistrationEmailJob.next_attempt_at <= now),
    )


def _finish(job_id, status, *, now, error_type=None, retry=False):
    job = db.session.get(RegistrationEmailJob, job_id)
    if job is None:
        return
    job.locked_at = None
    job.last_error_type = error_type
    if retry:
        job.attempts += 1
    if retry and job.attempts < current_app.config["REGISTRATION_EMAIL_MAX_ATTEMPTS"]:
        delay = min(300, 5 * (2 ** min(job.attempts - 1, 6)))
        job.status = "pending"
        job.next_attempt_at = now + timedelta(seconds=delay)
    else:
        job.status = status
        job.next_attempt_at = None
        if status == "completed":
            job.sent_at = now
    db.session.commit()


def process_pending_registration_emails(limit=50, mailer=None):
    """Claim and deliver durable registration messages without logging identities or codes."""
    mailer = mailer or current_app.extensions["recovery_mailer"]
    now = _now()
    candidate_ids = [row[0] for row in db.session.query(RegistrationEmailJob.id).filter(
        *_eligible(now)
    ).order_by(RegistrationEmailJob.id).limit(limit).all()]
    db.session.rollback()
    processed = 0
    for job_id in candidate_ids:
        now = _now()
        claimed = RegistrationEmailJob.query.filter(
            RegistrationEmailJob.id == job_id, *_eligible(now)
        ).update({"status": "processing", "locked_at": now}, synchronize_session=False)
        db.session.commit()
        if claimed != 1:
            continue
        job = db.session.get(RegistrationEmailJob, job_id)
        challenge = db.session.get(CustomerRegistrationChallenge, job.challenge_id) if job else None
        business = db.session.get(Business, job.business_id) if job else None
        if not challenge or not business or not business.is_active or challenge.consumed_at is not None or challenge.expires_at <= now:
            _finish(job_id, "cancelled", now=now)
            processed += 1
            continue
        try:
            code = _cipher().decrypt(job.encrypted_code.encode()).decode()
        except (InvalidToken, UnicodeError):
            _finish(job_id, "failed", now=now, error_type="InvalidPayload")
            processed += 1
            continue
        try:
            mailer.send_registration_code(challenge.email, code, business.name)
        except MailDeliveryError as exc:
            _finish(job_id, "failed", now=now, error_type=type(exc).__name__, retry=True)
        except Exception as exc:
            _finish(job_id, "failed", now=now, error_type=type(exc).__name__, retry=True)
        else:
            _finish(job_id, "completed", now=now)
        processed += 1
    return processed


def cleanup_registration_email_jobs(retention_seconds=None):
    retention = retention_seconds or current_app.config["REGISTRATION_EMAIL_RETENTION_SECONDS"]
    cutoff = _now() - timedelta(seconds=retention)
    deleted = RegistrationEmailJob.query.filter(
        RegistrationEmailJob.status.in_(("completed", "cancelled", "failed")),
        RegistrationEmailJob.updated_at < cutoff,
    ).delete(synchronize_session=False)
    db.session.commit()
    return deleted


def registration_email_counts():
    rows = dict(db.session.query(RegistrationEmailJob.status, db.func.count(RegistrationEmailJob.id)).group_by(
        RegistrationEmailJob.status
    ).all())
    return {key: rows.get(key, 0) for key in ("pending", "processing", "completed", "cancelled", "failed")}


def run_registration_email_worker(*, stop_event=None, sleep_fn=time.sleep, mailer=None,
                                  poll_seconds=5, batch_size=50, max_cycles=None, logger=None):
    stop_event = stop_event or threading.Event()
    logger = logger or logging.getLogger(__name__)
    cycles = 0
    while not stop_event.is_set():
        try:
            processed = process_pending_registration_emails(limit=batch_size, mailer=mailer)
            cleanup_registration_email_jobs()
            logger.info("registration_email_worker_metrics %s", json.dumps({
                "processed": processed, **registration_email_counts()
            }, sort_keys=True))
        except Exception as exc:
            db.session.rollback()
            logger.error("registration_email_worker_cycle_failed error_type=%s", type(exc).__name__)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        if sleep_fn is time.sleep:
            stop_event.wait(max(1, poll_seconds))
        else:
            sleep_fn(max(1, poll_seconds))
