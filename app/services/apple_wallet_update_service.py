import json
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, func, or_
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AccessAudit, AppleDevice, ApplePassCredential, AppleWalletPublication,
    AppleWalletPushJob, Business, Customer,
)
from app.services.apple_wallet import AppleWalletService
from app.services.apple_pass_update_sequence import allocate_apple_update_tag


ACTIVE = ("pending", "processing")
TERMINAL = ("completed", "failed", "superseded", "cancelled")
MAX_ATTEMPTS = 6


class ApplePublicationError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _now():
    return datetime.now(timezone.utc)


def _audit(action, publication, admin_id, previous, new):
    return AccessAudit(
        action=action, platform_admin_id=admin_id, business_id=publication.business_id,
        previous_state=json.dumps(previous, sort_keys=True),
        new_state=json.dumps(new, sort_keys=True),
    )


def enqueue_apple_publication(*, business, admin, reason, republish=False):
    if not isinstance(reason, str):
        raise ApplePublicationError("Indica un motivo válido para la publicación.")
    reason = reason.strip()
    if not reason or len(reason) > 255:
        raise ApplePublicationError("Indica un motivo válido para la publicación.")
    active = AppleWalletPublication.query.filter_by(business_id=business.id, active_key="active").first()
    if active:
        raise ApplePublicationError("Existe otra publicación Apple activa para este negocio.", 409)
    previous = AppleWalletPublication.query.filter_by(
        business_id=business.id, design_version=business.wallet_design_version,
    ).order_by(AppleWalletPublication.execution_number.desc()).first()
    if previous and not republish:
        raise ApplePublicationError("Esta versión ya tiene historial. Confirma la republicación.", 409)
    execution = (previous.execution_number + 1) if previous else 1
    publication = AppleWalletPublication(
        business_id=business.id, design_version=business.wallet_design_version,
        execution_number=execution, active_key="active", status="pending",
        created_by_admin_id=admin.id, reason=reason,
    )
    db.session.add(publication)
    db.session.flush()
    devices = AppleDevice.query.join(Customer, Customer.id == AppleDevice.customer_id).filter(
        Customer.business_id == business.id, AppleDevice.is_active.is_(True),
    ).all()
    customer_versions = {}
    for device in devices:
        credential = ApplePassCredential.query.filter_by(
            business_id=business.id, customer_id=device.customer_id,
        ).one_or_none()
        if not credential or credential.revoked_at is not None:
            continue
        if device.customer_id not in customer_versions:
            credential.update_version += 1
            credential.update_tag = allocate_apple_update_tag()
            credential.design_version = business.wallet_design_version
            customer_versions[device.customer_id] = credential.update_version
        db.session.add(AppleWalletPushJob(
            publication_id=publication.id, business_id=business.id,
            customer_id=device.customer_id, device_id=device.id,
            design_version=business.wallet_design_version,
            target_update_version=customer_versions[device.customer_id], reason="design",
        ))
        publication.total_jobs += 1
    db.session.add(_audit(
        "apple_wallet_publication_enqueued", publication, admin.id, {},
        {"publication_id": publication.id, "design_version": publication.design_version,
         "execution_number": execution, "total_jobs": publication.total_jobs},
    ))
    try:
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ApplePublicationError("Existe otra publicación Apple activa para este negocio.", 409) from exc
    return publication


def enqueue_apple_pass_update(customer, *, reason="balance"):
    credential = ApplePassCredential.query.filter_by(
        business_id=customer.business_id, customer_id=customer.id,
    ).one_or_none()
    if not credential or credential.revoked_at is not None:
        return 0
    credential.update_version += 1
    credential.update_tag = allocate_apple_update_tag()
    credential.design_version = customer.business.wallet_design_version
    count = 0
    for device in AppleDevice.query.filter_by(customer_id=customer.id, is_active=True).all():
        db.session.add(AppleWalletPushJob(
            business_id=customer.business_id, customer_id=customer.id, device_id=device.id,
            design_version=credential.design_version, target_update_version=credential.update_version,
            reason=reason,
        ))
        count += 1
    return count


def cancel_apple_publication(publication, admin):
    changed = AppleWalletPublication.query.filter(
        AppleWalletPublication.id == publication.id,
        AppleWalletPublication.status.in_(ACTIVE),
    ).update({"status": "cancelled", "active_key": None}, synchronize_session=False)
    if not changed:
        raise ApplePublicationError("Solo pueden cancelarse publicaciones Apple activas.", 409)
    AppleWalletPushJob.query.filter(
        AppleWalletPushJob.publication_id == publication.id,
        AppleWalletPushJob.status.in_(ACTIVE),
    ).update({"status": "cancelled", "lease_token": None, "locked_at": None}, synchronize_session=False)
    db.session.add(_audit("apple_wallet_publication_cancelled", publication, admin.id,
                          {"status": publication.status}, {"status": "cancelled"}))
    db.session.commit()


def _claim_job(job_id, stale_before):
    token = secrets.token_hex(24)
    changed = AppleWalletPushJob.query.filter(
        AppleWalletPushJob.id == job_id,
        or_(AppleWalletPushJob.status == "pending",
            and_(AppleWalletPushJob.status == "processing", AppleWalletPushJob.locked_at <= stale_before)),
        or_(AppleWalletPushJob.next_attempt_at.is_(None), AppleWalletPushJob.next_attempt_at <= _now()),
    ).update({"status": "processing", "lease_token": token, "locked_at": _now()}, synchronize_session=False)
    db.session.commit()
    return token if changed else None


def process_pending_apple_wallet_jobs(limit=50, service=None):
    service = service or AppleWalletService()
    stale_before = _now() - timedelta(minutes=5)
    ids = [row[0] for row in db.session.query(AppleWalletPushJob.id).filter(
        or_(AppleWalletPushJob.status == "pending",
            and_(AppleWalletPushJob.status == "processing", AppleWalletPushJob.locked_at <= stale_before)),
        or_(AppleWalletPushJob.next_attempt_at.is_(None), AppleWalletPushJob.next_attempt_at <= _now()),
    ).order_by(AppleWalletPushJob.id).limit(limit).all()]
    processed = 0
    for job_id in ids:
        token = _claim_job(job_id, stale_before)
        if not token:
            continue
        job = db.session.get(AppleWalletPushJob, job_id)
        device = db.session.get(AppleDevice, job.device_id)
        business = db.session.get(Business, job.business_id)
        credential = ApplePassCredential.query.filter_by(
            business_id=job.business_id, customer_id=job.customer_id,
        ).one_or_none()
        if (not device or not device.is_active or not business or not business.is_active or
                device.customer_id != job.customer_id or
                not credential or credential.revoked_at is not None or
                device.pass_type_identifier != credential.pass_type_identifier or
                device.serial_number != credential.serial_number or
                job.target_update_version > credential.update_version or
                (job.publication_id and business.wallet_design_version != job.design_version)):
            final = "superseded" if job.publication_id else "cancelled"
            AppleWalletPushJob.query.filter_by(id=job_id, status="processing", lease_token=token).update(
                {"status": final, "lease_token": None, "locked_at": None}, synchronize_session=False)
            db.session.commit()
            processed += 1
            continue
        push_token = device.push_token
        db.session.rollback()  # never keep the ORM transaction open during APNs.
        result = service.send_push_notification(push_token)
        job = db.session.get(AppleWalletPushJob, job_id)
        if not job or job.status != "processing" or job.lease_token != token:
            db.session.rollback()
            continue
        values = {"attempts": job.attempts + 1, "last_status_code": result.get("status_code"),
                  "lease_token": None, "locked_at": None}
        if result.get("ok"):
            values.update(status="completed", next_attempt_at=None)
            AppleDevice.query.filter_by(
                id=job.device_id, customer_id=job.customer_id, is_active=True,
            ).update(
                {"last_notified_version": case(
                    (AppleDevice.last_notified_version < job.target_update_version, job.target_update_version),
                    else_=AppleDevice.last_notified_version,
                )},
                synchronize_session=False,
            )
        elif result.get("permanent"):
            values.update(status="failed", next_attempt_at=None)
            if result.get("status_code") == 410:
                AppleDevice.query.filter_by(id=job.device_id, customer_id=job.customer_id).update(
                    {"is_active": False, "invalidated_at": _now()}, synchronize_session=False)
        elif values["attempts"] >= MAX_ATTEMPTS:
            values.update(status="failed", next_attempt_at=None)
        else:
            values.update(status="pending", next_attempt_at=_now() + timedelta(seconds=min(3600, 2 ** values["attempts"] * 15)))
        AppleWalletPushJob.query.filter_by(id=job_id, status="processing", lease_token=token).update(values, synchronize_session=False)
        db.session.commit()
        processed += 1
    _finish_publications()
    return processed


def _finish_publications():
    for publication in AppleWalletPublication.query.filter(AppleWalletPublication.status.in_(ACTIVE)).all():
        statuses = {row[0] for row in db.session.query(AppleWalletPushJob.status).filter_by(publication_id=publication.id).all()}
        if statuses & {"pending", "processing"}:
            continue
        publication.status = ("failed" if "failed" in statuses else
                              "superseded" if "superseded" in statuses else "completed")
        publication.active_key = None
    db.session.commit()


def apple_publication_summary(business_id):
    publication = AppleWalletPublication.query.filter_by(business_id=business_id).order_by(AppleWalletPublication.id.desc()).first()
    if not publication:
        return None
    counts = dict(db.session.query(AppleWalletPushJob.status, func.count(AppleWalletPushJob.id)).filter_by(
        publication_id=publication.id).group_by(AppleWalletPushJob.status).all())
    return {"publication": publication, "counts": counts}
