import secrets
from datetime import timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError

from app.access_audit import access_audit
from app.extensions import db
from app.models import Business, Customer, GoogleWalletPublication, GoogleWalletPublicationJob
from app.services.google_wallet import GoogleWalletService
from app.services.google_wallet_sync_service import _customer_lock, _now


LEASE_SECONDS = 300
MAX_ATTEMPTS = 6
ACTIVE_STATUSES = ("pending", "processing")
TERMINAL_STATUSES = ("completed", "failed", "superseded", "cancelled")


class WalletPublicationError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _supersede_publication(publication_id):
    changed = GoogleWalletPublication.query.filter(
        GoogleWalletPublication.id == publication_id,
        GoogleWalletPublication.status.in_(ACTIVE_STATUSES + ("failed",)),
    ).update({
        "status": "superseded", "active_key": None, "locked_at": None,
        "lease_token": None, "next_attempt_at": None,
    }, synchronize_session=False)
    if changed:
        GoogleWalletPublicationJob.query.filter(
            GoogleWalletPublicationJob.publication_id == publication_id,
            GoogleWalletPublicationJob.status.in_(ACTIVE_STATUSES + ("failed",)),
        ).update({
            "status": "superseded", "locked_at": None,
            "lease_token": None, "next_attempt_at": None,
        }, synchronize_session=False)
    return bool(changed)


def enqueue_wallet_publication(*, business, admin, provider_mode, reason, republish=False):
    if not isinstance(reason, str):
        raise WalletPublicationError("Indica un motivo válido para la publicación.")
    reason = reason.strip()
    if provider_mode == "disabled":
        raise WalletPublicationError("Google Wallet está deshabilitado.", 409)
    if provider_mode not in {"mock", "real"}:
        raise WalletPublicationError("El modo de Google Wallet no es válido.", 409)
    if not reason or len(reason) > 255:
        raise WalletPublicationError("Indica un motivo válido para la publicación.")
    if not isinstance(republish, bool):
        raise WalletPublicationError("La confirmación de republicación no es válida.")

    active = GoogleWalletPublication.query.filter_by(
        business_id=business.id, active_key="active",
    ).one_or_none()
    if active and active.design_version == business.wallet_design_version:
        return active, True
    if active:
        _supersede_publication(active.id)

    previous = GoogleWalletPublication.query.filter_by(
        business_id=business.id, design_version=business.wallet_design_version,
    ).order_by(GoogleWalletPublication.execution_number.desc()).first()
    if previous and previous.status in TERMINAL_STATUSES and not republish:
        db.session.rollback()
        raise WalletPublicationError(
            "Esta versión ya tuvo una ejecución. Confirma explícitamente la republicación.", 409,
        )
    execution_number = (previous.execution_number + 1) if previous else 1
    publication = GoogleWalletPublication(
        business_id=business.id, design_version=business.wallet_design_version,
        execution_number=execution_number, active_key="active",
        provider_mode=provider_mode, created_by_admin_id=admin.id, reason=reason,
        total_jobs=business.customers.count(),
    )
    db.session.add(publication)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        active = GoogleWalletPublication.query.filter_by(
            business_id=business.id, active_key="active",
        ).one_or_none()
        if active and active.design_version == business.wallet_design_version:
            return active, True
        raise WalletPublicationError("Existe otra publicación activa para este negocio.", 409)
    customer_ids = [row[0] for row in db.session.query(Customer.id).filter_by(
        business_id=business.id,
    ).all()]
    db.session.add_all([
        GoogleWalletPublicationJob(
            publication_id=publication.id, business_id=business.id,
            customer_id=customer_id, design_version=business.wallet_design_version,
        ) for customer_id in customer_ids
    ])
    action = "wallet_publication_republished" if previous else "wallet_publication_enqueued"
    db.session.add(access_audit(
        action, platform_admin_id=admin.id, business_id=business.id,
        new={"publication_id": publication.id, "design_version": publication.design_version,
             "execution_number": execution_number, "provider_mode": provider_mode,
             "total_jobs": publication.total_jobs},
    ))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        active = GoogleWalletPublication.query.filter_by(
            business_id=business.id, active_key="active",
        ).one_or_none()
        if active and active.design_version == business.wallet_design_version:
            return active, True
        raise WalletPublicationError("Existe otra publicación activa para este negocio.", 409)
    return publication, False


def retry_wallet_publication(*, publication, admin):
    if publication.status != "failed":
        raise WalletPublicationError("Solo pueden reintentarse publicaciones fallidas.", 409)
    business = db.session.get(Business, publication.business_id)
    if not business or publication.design_version != business.wallet_design_version:
        raise WalletPublicationError("La publicación fue reemplazada por una versión nueva.", 409)
    publication.status = "pending"
    publication.active_key = "active"
    publication.next_attempt_at = None
    publication.locked_at = None
    publication.lease_token = None
    publication.attempts = 0
    publication.last_status_code = None
    GoogleWalletPublicationJob.query.filter_by(
        publication_id=publication.id, status="failed",
    ).update({
        "status": "pending", "next_attempt_at": None, "locked_at": None,
        "lease_token": None, "attempts": 0,
    }, synchronize_session=False)
    db.session.add(access_audit(
        "wallet_publication_retried", platform_admin_id=admin.id,
        business_id=publication.business_id,
        new={"publication_id": publication.id, "design_version": publication.design_version,
             "execution_number": publication.execution_number},
    ))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise WalletPublicationError("Existe otra publicación activa para este negocio.", 409)
    return publication


def cancel_wallet_publication(*, publication, admin):
    changed = GoogleWalletPublication.query.filter(
        GoogleWalletPublication.id == publication.id,
        GoogleWalletPublication.business_id == publication.business_id,
        GoogleWalletPublication.status.in_(ACTIVE_STATUSES),
    ).update({
        "status": "cancelled", "active_key": None, "locked_at": None,
        "lease_token": None, "next_attempt_at": None,
    }, synchronize_session=False)
    if not changed:
        db.session.rollback()
        raise WalletPublicationError("Solo pueden cancelarse publicaciones pendientes.", 409)
    GoogleWalletPublicationJob.query.filter(
        GoogleWalletPublicationJob.publication_id == publication.id,
        GoogleWalletPublicationJob.status.in_(ACTIVE_STATUSES),
    ).update({
        "status": "cancelled", "locked_at": None,
        "lease_token": None, "next_attempt_at": None,
    }, synchronize_session=False)
    db.session.add(access_audit(
        "wallet_publication_cancelled", platform_admin_id=admin.id,
        business_id=publication.business_id,
        new={"publication_id": publication.id, "design_version": publication.design_version,
             "execution_number": publication.execution_number},
    ))
    db.session.commit()
    return publication


def _claim_publication(publication_id, now):
    token = secrets.token_hex(24)
    stale_before = now - timedelta(seconds=LEASE_SECONDS)
    changed = GoogleWalletPublication.query.filter(
        GoogleWalletPublication.id == publication_id,
        GoogleWalletPublication.active_key == "active",
        or_(
            GoogleWalletPublication.status == "pending",
            and_(GoogleWalletPublication.status == "processing",
                 GoogleWalletPublication.locked_at <= stale_before),
        ),
        or_(GoogleWalletPublication.next_attempt_at.is_(None),
            GoogleWalletPublication.next_attempt_at <= now),
    ).update({
        "status": "processing", "locked_at": now, "lease_token": token,
    }, synchronize_session=False)
    db.session.commit()
    return token if changed == 1 else None


def _claim_job(job_id, publication_id, publication_token, now):
    token = secrets.token_hex(24)
    stale_before = now - timedelta(seconds=LEASE_SECONDS)
    parent_owned = db.session.query(GoogleWalletPublication.id).filter(
        GoogleWalletPublication.id == publication_id,
        GoogleWalletPublication.status == "processing",
        GoogleWalletPublication.lease_token == publication_token,
        GoogleWalletPublication.active_key == "active",
    ).exists()
    changed = GoogleWalletPublicationJob.query.filter(
        GoogleWalletPublicationJob.id == job_id,
        parent_owned,
        or_(
            GoogleWalletPublicationJob.status == "pending",
            and_(GoogleWalletPublicationJob.status == "processing",
                 GoogleWalletPublicationJob.locked_at <= stale_before),
        ),
        or_(GoogleWalletPublicationJob.next_attempt_at.is_(None),
            GoogleWalletPublicationJob.next_attempt_at <= now),
    ).update({
        "status": "processing", "locked_at": now, "lease_token": token,
    }, synchronize_session=False)
    db.session.commit()
    return token if changed == 1 else None


def _publication_is_owned(publication_id, token, design_version):
    return db.session.query(GoogleWalletPublication.id).join(
        Business, Business.id == GoogleWalletPublication.business_id,
    ).filter(
        GoogleWalletPublication.id == publication_id,
        GoogleWalletPublication.status == "processing",
        GoogleWalletPublication.active_key == "active",
        GoogleWalletPublication.lease_token == token,
        Business.wallet_design_version == design_version,
    ).first() is not None


def _job_is_owned(job_id, token, publication_id, publication_token):
    return db.session.query(GoogleWalletPublicationJob.id).join(
        GoogleWalletPublication,
        GoogleWalletPublication.id == GoogleWalletPublicationJob.publication_id,
    ).filter(
        GoogleWalletPublicationJob.id == job_id,
        GoogleWalletPublicationJob.status == "processing",
        GoogleWalletPublicationJob.lease_token == token,
        GoogleWalletPublication.id == publication_id,
        GoogleWalletPublication.status == "processing",
        GoogleWalletPublication.lease_token == publication_token,
        GoogleWalletPublication.active_key == "active",
    ).first() is not None


def _store_remote_result(model, model_id, token, result):
    query = model.query.filter(
        model.id == model_id, model.status == "processing", model.lease_token == token,
    )
    status = result.get("status")
    if status in {"success", "mock"}:
        values = {"status": "completed", "last_status_code": None,
                  "locked_at": None, "lease_token": None}
    elif status == "skipped":
        values = {"status": "skipped", "last_status_code": result.get("status_code"),
                  "locked_at": None, "lease_token": None}
    elif status == "retry":
        current = query.with_entities(model.attempts).scalar()
        if current is None:
            return False
        attempts = current + 1
        if attempts >= MAX_ATTEMPTS:
            values = {"status": "failed", "attempts": attempts,
                      "last_status_code": result.get("status_code"),
                      "locked_at": None, "lease_token": None, "next_attempt_at": None}
        else:
            values = {"status": "pending", "attempts": attempts,
                      "last_status_code": result.get("status_code"),
                      "locked_at": None, "lease_token": None,
                      "next_attempt_at": _now() + timedelta(
                          seconds=min(3600, 30 * (2 ** min(attempts, 6))))}
    else:
        current = query.with_entities(model.attempts).scalar()
        if current is None:
            return False
        values = {"status": "failed", "attempts": current + 1,
                  "last_status_code": result.get("status_code"),
                  "locked_at": None, "lease_token": None, "next_attempt_at": None}
    if model is GoogleWalletPublication and values["status"] in {"completed", "failed"}:
        values["active_key"] = None
    return query.update(values, synchronize_session=False) == 1


def _finish_publication(publication_id, token):
    publication = GoogleWalletPublication.query.filter_by(id=publication_id).first()
    if not publication or publication.status != "processing" or publication.lease_token != token:
        db.session.rollback()
        return
    statuses = dict(db.session.query(
        GoogleWalletPublicationJob.status, func.count(GoogleWalletPublicationJob.id),
    ).filter_by(publication_id=publication_id).group_by(GoogleWalletPublicationJob.status).all())
    if statuses.get("failed"):
        status = "failed"
    elif statuses.get("pending") or statuses.get("processing"):
        status = "pending"
    else:
        status = "completed"
    values = {"status": status, "locked_at": None, "lease_token": None}
    if status in {"completed", "failed"}:
        values["active_key"] = None
    GoogleWalletPublication.query.filter(
        GoogleWalletPublication.id == publication_id,
        GoogleWalletPublication.status == "processing",
        GoogleWalletPublication.lease_token == token,
    ).update(values, synchronize_session=False)
    db.session.commit()


def process_pending_wallet_publications(limit=50, service=None):
    service = service or GoogleWalletService()
    if getattr(service, "mode", None) == "disabled":
        return 0
    now = _now()
    stale_before = now - timedelta(seconds=LEASE_SECONDS)
    candidate_ids = [row[0] for row in db.session.query(GoogleWalletPublication.id).filter(
        GoogleWalletPublication.active_key == "active",
        or_(
            GoogleWalletPublication.status == "pending",
            and_(GoogleWalletPublication.status == "processing",
                 GoogleWalletPublication.locked_at <= stale_before),
        ),
        or_(GoogleWalletPublication.next_attempt_at.is_(None),
            GoogleWalletPublication.next_attempt_at <= now),
    ).order_by(GoogleWalletPublication.id).limit(limit).all()]
    processed = 0
    for publication_id in candidate_ids:
        publication_token = _claim_publication(publication_id, _now())
        if not publication_token:
            continue
        publication = db.session.get(GoogleWalletPublication, publication_id)
        business = db.session.get(Business, publication.business_id)
        if not business or business.wallet_design_version != publication.design_version:
            _supersede_publication(publication.id); db.session.commit(); continue
        if publication.provider_mode != getattr(service, "mode", None):
            _store_remote_result(
                GoogleWalletPublication, publication.id, publication_token,
                {"status": "error", "status_code": None},
            )
            GoogleWalletPublication.query.filter_by(id=publication.id, status="failed").update(
                {"active_key": None}, synchronize_session=False,
            )
            db.session.commit(); continue
        if not publication.class_published:
            prepared_class = (
                service.create_or_update_class(business)
                if isinstance(service, GoogleWalletService) else None
            )
            owned = _publication_is_owned(publication.id, publication_token, publication.design_version)
            db.session.rollback()
            if not owned:
                db.session.rollback(); continue
            result = (
                service.publish_loyalty_class(business, prepared=prepared_class)
                if prepared_class is not None else service.publish_loyalty_class(business)
            )
            if result.get("status") in {"success", "mock"}:
                GoogleWalletPublication.query.filter(
                    GoogleWalletPublication.id == publication.id,
                    GoogleWalletPublication.status == "processing",
                    GoogleWalletPublication.lease_token == publication_token,
                ).update({"class_published": True}, synchronize_session=False)
                db.session.commit()
            else:
                _store_remote_result(GoogleWalletPublication, publication.id, publication_token, result)
                db.session.expire_all()
                current = db.session.get(GoogleWalletPublication, publication.id)
                if current and current.status == "failed":
                    current.active_key = None
                db.session.commit(); continue
        if not _publication_is_owned(publication.id, publication_token, publication.design_version):
            db.session.rollback(); continue
        job_ids = [row[0] for row in db.session.query(GoogleWalletPublicationJob.id).filter(
            GoogleWalletPublicationJob.publication_id == publication.id,
            or_(
                GoogleWalletPublicationJob.status == "pending",
                and_(GoogleWalletPublicationJob.status == "processing",
                     GoogleWalletPublicationJob.locked_at <= stale_before),
            ),
            or_(GoogleWalletPublicationJob.next_attempt_at.is_(None),
                GoogleWalletPublicationJob.next_attempt_at <= now),
        ).order_by(GoogleWalletPublicationJob.id).limit(max(0, limit - processed)).all()]
        for job_id in job_ids:
            job_token = _claim_job(job_id, publication.id, publication_token, _now())
            if not job_token:
                continue
            job = db.session.get(GoogleWalletPublicationJob, job_id)
            with _customer_lock(job.customer_id) as acquired:
                if not acquired:
                    GoogleWalletPublicationJob.query.filter_by(
                        id=job.id, status="processing", lease_token=job_token,
                    ).update({"status": "pending", "locked_at": None, "lease_token": None},
                             synchronize_session=False)
                    db.session.commit(); continue
                customer = db.session.get(Customer, job.customer_id)
                prepared_object = (
                    service.create_loyalty_object(customer)
                    if customer is not None and isinstance(service, GoogleWalletService) else None
                )
                owned = _job_is_owned(job.id, job_token, publication.id, publication_token)
                db.session.rollback()
                if not owned:
                    db.session.rollback(); continue
                if customer is None:
                    result = {"status": "error", "status_code": None}
                elif prepared_object is not None:
                    result = service.publish_loyalty_object_metadata(customer, prepared=prepared_object)
                else:
                    result = service.publish_loyalty_object_metadata(customer)
                _store_remote_result(GoogleWalletPublicationJob, job.id, job_token, result)
                db.session.commit(); processed += 1
        _finish_publication(publication.id, publication_token)
        if processed >= limit:
            break
    return processed


def wallet_publication_summary(business_id):
    publication = GoogleWalletPublication.query.filter_by(business_id=business_id).order_by(
        GoogleWalletPublication.id.desc(),
    ).first()
    if not publication:
        return None
    counts = dict(db.session.query(
        GoogleWalletPublicationJob.status, func.count(GoogleWalletPublicationJob.id),
    ).filter_by(publication_id=publication.id).group_by(GoogleWalletPublicationJob.status).all())
    return {"publication": publication, "counts": counts}


def wallet_publication_metrics():
    now = _now()
    stale_before = now - timedelta(seconds=LEASE_SECONDS)
    statuses = dict(db.session.query(
        GoogleWalletPublicationJob.status, func.count(GoogleWalletPublicationJob.id),
    ).group_by(GoogleWalletPublicationJob.status).all())
    return {
        "pending": statuses.get("pending", 0),
        "processing": statuses.get("processing", 0),
        "completed": statuses.get("completed", 0),
        "skipped": statuses.get("skipped", 0),
        "failed": statuses.get("failed", 0),
        "stale_processing": GoogleWalletPublicationJob.query.filter(
            GoogleWalletPublicationJob.status == "processing",
            GoogleWalletPublicationJob.locked_at <= stale_before,
        ).count(),
    }
