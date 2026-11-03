from datetime import timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError

from app.access_audit import access_audit
from app.extensions import db
from app.models import (
    Business, Customer, GoogleWalletPublication, GoogleWalletPublicationJob,
)
from app.services.google_wallet import GoogleWalletService
from app.services.google_wallet_sync_service import _customer_lock, _now


class WalletPublicationError(ValueError):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def enqueue_wallet_publication(*, business, admin, provider_mode, reason):
    reason = (reason or "").strip()
    if provider_mode == "disabled":
        raise WalletPublicationError("Google Wallet está deshabilitado.", 409)
    if not reason or len(reason) > 255:
        raise WalletPublicationError("Indica un motivo válido para la publicación.")
    existing = GoogleWalletPublication.query.filter_by(
        business_id=business.id, design_version=business.wallet_design_version,
    ).one_or_none()
    if existing:
        return existing, True

    replaced_ids = [row[0] for row in db.session.query(GoogleWalletPublication.id).filter(
        GoogleWalletPublication.business_id == business.id,
        GoogleWalletPublication.status.in_(("pending", "processing", "failed")),
    ).all()]
    if replaced_ids:
        GoogleWalletPublication.query.filter(
            GoogleWalletPublication.id.in_(replaced_ids),
        ).update({"status": "superseded", "locked_at": None}, synchronize_session=False)
        GoogleWalletPublicationJob.query.filter(
            GoogleWalletPublicationJob.publication_id.in_(replaced_ids),
            GoogleWalletPublicationJob.status.in_(("pending", "processing", "failed")),
        ).update({"status": "superseded", "locked_at": None}, synchronize_session=False)
    publication = GoogleWalletPublication(
        business_id=business.id, design_version=business.wallet_design_version,
        provider_mode=provider_mode, created_by_admin_id=admin.id, reason=reason,
        total_jobs=business.customers.count(),
    )
    db.session.add(publication)
    db.session.flush()
    for (customer_id,) in db.session.query(Customer.id).filter_by(business_id=business.id).yield_per(500):
        db.session.add(GoogleWalletPublicationJob(
            publication_id=publication.id, business_id=business.id,
            customer_id=customer_id, design_version=business.wallet_design_version,
        ))
    db.session.add(access_audit(
        "wallet_publication_enqueued", platform_admin_id=admin.id, business_id=business.id,
        new={"publication_id": publication.id, "design_version": publication.design_version,
             "provider_mode": provider_mode, "total_jobs": publication.total_jobs},
    ))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existing = GoogleWalletPublication.query.filter_by(
            business_id=business.id, design_version=business.wallet_design_version,
        ).one_or_none()
        if existing:
            return existing, True
        raise
    return publication, False


def retry_wallet_publication(*, publication, admin):
    if publication.status not in {"failed"}:
        raise WalletPublicationError("Solo pueden reintentarse publicaciones fallidas.", 409)
    business = db.session.get(Business, publication.business_id)
    if not business or publication.design_version != business.wallet_design_version:
        raise WalletPublicationError("La publicación fue reemplazada por una versión nueva.", 409)
    publication.status = "pending"
    publication.next_attempt_at = None
    publication.locked_at = None
    GoogleWalletPublicationJob.query.filter_by(
        publication_id=publication.id, status="failed",
    ).update({"status": "pending", "next_attempt_at": None, "locked_at": None}, synchronize_session=False)
    db.session.add(access_audit(
        "wallet_publication_retried", platform_admin_id=admin.id,
        business_id=publication.business_id,
        new={"publication_id": publication.id, "design_version": publication.design_version},
    ))
    db.session.commit()
    return publication


def cancel_wallet_publication(*, publication, admin):
    if publication.status not in {"pending", "processing"}:
        raise WalletPublicationError("Solo pueden cancelarse publicaciones pendientes.", 409)
    publication.status = "cancelled"
    publication.locked_at = None
    publication.next_attempt_at = None
    GoogleWalletPublicationJob.query.filter_by(publication_id=publication.id).filter(
        GoogleWalletPublicationJob.status.in_(("pending", "processing")),
    ).update(
        {"status": "cancelled", "locked_at": None, "next_attempt_at": None},
        synchronize_session=False,
    )
    db.session.add(access_audit(
        "wallet_publication_cancelled", platform_admin_id=admin.id,
        business_id=publication.business_id,
        new={"publication_id": publication.id, "design_version": publication.design_version},
    ))
    db.session.commit()
    return publication


def _defer(target, status_code=None):
    target.attempts += 1
    target.last_status_code = status_code
    target.locked_at = None
    target.status = "pending"
    target.next_attempt_at = _now() + timedelta(seconds=min(3600, 30 * (2 ** min(target.attempts, 6))))


def _finish_publication(publication):
    statuses = dict(db.session.query(
        GoogleWalletPublicationJob.status, func.count(GoogleWalletPublicationJob.id),
    ).filter_by(publication_id=publication.id).group_by(GoogleWalletPublicationJob.status).all())
    if statuses.get("failed"):
        publication.status = "failed"
    elif statuses.get("pending") or statuses.get("processing"):
        publication.status = "pending"
    else:
        publication.status = "completed"
    publication.locked_at = None


def process_pending_wallet_publications(limit=50, service=None):
    service = service or GoogleWalletService()
    if getattr(service, "mode", None) == "disabled":
        return 0
    now = _now()
    stale_before = now - timedelta(minutes=5)
    publications = GoogleWalletPublication.query.filter(
        or_(
            GoogleWalletPublication.status == "pending",
            and_(
                GoogleWalletPublication.status == "processing",
                GoogleWalletPublication.locked_at <= stale_before,
            ),
        ),
        or_(GoogleWalletPublication.next_attempt_at.is_(None),
            GoogleWalletPublication.next_attempt_at <= now),
    ).order_by(GoogleWalletPublication.id).limit(limit).all()
    processed = 0
    for publication in publications:
        if publication.provider_mode != getattr(service, "mode", None):
            publication.status = "failed"
            publication.last_status_code = None
            publication.locked_at = None
            db.session.commit()
            continue
        business = db.session.get(Business, publication.business_id)
        if not business or business.wallet_design_version != publication.design_version:
            publication.status = "superseded"
            GoogleWalletPublicationJob.query.filter_by(publication_id=publication.id).filter(
                GoogleWalletPublicationJob.status.in_(("pending", "processing", "failed")),
            ).update({"status": "superseded", "locked_at": None}, synchronize_session=False)
            db.session.commit()
            continue
        publication.status = "processing"
        publication.locked_at = now
        if not publication.class_published:
            result = service.publish_loyalty_class(business)
            if result.get("status") in {"success", "mock"}:
                publication.class_published = True
            elif result.get("status") == "retry":
                _defer(publication, result.get("status_code")); db.session.commit(); continue
            else:
                publication.status = "failed"; publication.attempts += 1
                publication.last_status_code = result.get("status_code"); publication.locked_at = None
                db.session.commit(); continue
        jobs = GoogleWalletPublicationJob.query.filter(
            GoogleWalletPublicationJob.publication_id == publication.id,
            or_(
                GoogleWalletPublicationJob.status == "pending",
                and_(
                    GoogleWalletPublicationJob.status == "processing",
                    GoogleWalletPublicationJob.locked_at <= stale_before,
                ),
            ),
        ).filter(or_(
            GoogleWalletPublicationJob.next_attempt_at.is_(None),
            GoogleWalletPublicationJob.next_attempt_at <= now,
        )).order_by(GoogleWalletPublicationJob.id).limit(max(0, limit - processed)).all()
        for job in jobs:
            with _customer_lock(job.customer_id) as acquired:
                if not acquired:
                    db.session.rollback(); continue
                db.session.refresh(publication)
                db.session.refresh(business)
                if publication.status == "superseded" or business.wallet_design_version != publication.design_version:
                    job.status = "superseded"; db.session.commit(); continue
                job.status = "processing"; job.locked_at = _now(); db.session.flush()
                customer = db.session.get(Customer, job.customer_id)
                result = service.publish_loyalty_object_metadata(customer) if customer else {"status": "error"}
                if result.get("status") in {"success", "mock"}:
                    job.status = "completed"; job.last_status_code = None; job.locked_at = None
                elif result.get("status") == "retry":
                    _defer(job, result.get("status_code"))
                else:
                    job.status = "failed"; job.attempts += 1
                    job.last_status_code = result.get("status_code"); job.locked_at = None
                db.session.commit(); processed += 1
        _finish_publication(publication)
        db.session.commit()
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
