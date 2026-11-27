"""Run the real referral processor under a restricted synthetic database login."""

from sqlalchemy import text

from app.extensions import db
from app.models import Business
from app.services.google_wallet_referral_queue import process_wallet_referral_jobs
from app.services.google_wallet_write_reservation import reserve, transition
from run import app


class ProviderSimulation:
    mode = "real"

    def publish_referral_links(self, customer):
        assert customer.business_id == 1
        identity = dict(kind="loyaltyObject", issuer="synthetic",
                        resource_id=f"synthetic.business_1_customer_{customer.id}",
                        business_id=1, customer_id=customer.id)
        operation = reserve(db.engine, **identity)
        assert operation is not None
        assert reserve(db.engine, **identity) is None
        for previous, state in (("reserved", "started"), ("started", "confirmed"),
                                ("confirmed", "completed")):
            assert transition(db.engine, operation, previous=previous, state=state)
            assert not transition(db.engine, operation, previous=previous, state=state)
        return {"status": "success"}


with app.app_context():
    role = db.session.scalar(text("SELECT current_user"))
    if role == "j2m_schema_web":
        business = db.session.get(Business, 1)
        business.referrals_enabled = not business.referrals_enabled
        db.session.commit()
        assert db.session.scalar(text("SELECT count(*) FROM google_wallet_referral_jobs")) == 1
    elif role == "j2m_schema_worker":
        app.config["GOOGLE_WALLET_MODE"] = "real"
        assert process_wallet_referral_jobs(service=ProviderSimulation()) == 1
        assert process_wallet_referral_jobs(service=ProviderSimulation()) == 1
        assert process_wallet_referral_jobs(service=ProviderSimulation()) == 0
        assert db.session.scalar(text(
            "SELECT count(*) FROM google_wallet_referral_jobs WHERE status='completed' AND business_id=1"
        )) == 2
    else:
        raise AssertionError("Unexpected database role")
