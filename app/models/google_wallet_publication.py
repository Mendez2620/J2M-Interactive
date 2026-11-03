from datetime import datetime, timezone

from app.extensions import db


def _utcnow():
    return datetime.now(timezone.utc)


class GoogleWalletPublication(db.Model):
    __tablename__ = "google_wallet_publications"
    __table_args__ = (
        db.UniqueConstraint("business_id", "design_version", name="uq_google_wallet_publication_version"),
        db.UniqueConstraint("business_id", "id", name="uq_google_wallet_publication_business_id_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    business_id = db.Column(db.Integer, db.ForeignKey("businesses.id", ondelete="RESTRICT"), nullable=False, index=True)
    design_version = db.Column(db.Integer, nullable=False)
    provider_mode = db.Column(db.String(20), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", server_default="pending", index=True)
    created_by_admin_id = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255), nullable=False)
    total_jobs = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    class_published = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = db.Column(db.DateTime, nullable=True, index=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    last_status_code = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)


class GoogleWalletPublicationJob(db.Model):
    __tablename__ = "google_wallet_publication_jobs"
    __table_args__ = (
        db.ForeignKeyConstraint(
            ["business_id", "publication_id"],
            ["google_wallet_publications.business_id", "google_wallet_publications.id"],
            ondelete="RESTRICT", name="fk_google_wallet_publication_job_batch_tenant",
        ),
        db.ForeignKeyConstraint(
            ["business_id", "customer_id"], ["customers.business_id", "customers.id"],
            ondelete="RESTRICT", name="fk_google_wallet_publication_job_customer_tenant",
        ),
        db.UniqueConstraint("publication_id", "customer_id", name="uq_google_wallet_publication_job_customer"),
    )

    id = db.Column(db.Integer, primary_key=True)
    publication_id = db.Column(db.Integer, nullable=False, index=True)
    business_id = db.Column(db.Integer, nullable=False, index=True)
    customer_id = db.Column(db.Integer, nullable=False, index=True)
    design_version = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending", server_default="pending", index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = db.Column(db.DateTime, nullable=True, index=True)
    locked_at = db.Column(db.DateTime, nullable=True)
    last_status_code = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=_utcnow, onupdate=_utcnow)
