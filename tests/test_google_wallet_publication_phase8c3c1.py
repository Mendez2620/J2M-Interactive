import unittest
from datetime import timedelta

from sqlalchemy import text

from app import create_app
from app.extensions import db
from app.models import (
    AccessAudit, Business, Customer, GoogleWalletPublication,
    GoogleWalletPublicationJob, LoyaltyLedgerEntry, PlatformAdmin, Transaction,
)
from app.services.google_wallet_publication_service import (
    process_pending_wallet_publications,
)
from app.services.google_wallet_sync_service import _customer_lock
from app.services.google_wallet_sync_service import _now


class MockPublisher:
    mode = "mock"

    def __init__(self, object_results=None, class_result=None):
        self.class_calls = []
        self.object_calls = []
        self.object_results = list(object_results or [])
        self.class_result = class_result or {"status": "mock"}

    def publish_loyalty_class(self, business):
        self.class_calls.append(business.id)
        return self.class_result

    def publish_loyalty_object_metadata(self, customer):
        self.object_calls.append(customer.id)
        return self.object_results.pop(0) if self.object_results else {"status": "mock"}


class GoogleWalletPublicationPhase8C3C1TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(GOOGLE_WALLET_MODE="mock", CSRF_ENABLED=True)
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all(); db.session.execute(text("PRAGMA foreign_keys=ON"))
        self.client = self.app.test_client()
        self.business = Business(name="Wallet Uno", slug="wallet-uno", loyalty_type="points")
        self.other = Business(name="Wallet Dos", slug="wallet-dos", loyalty_type="stamps")
        db.session.add_all([self.business, self.other]); db.session.flush()
        self.customer = Customer(
            business_id=self.business.id, full_name="Cliente", phone="+525500000001",
            current_points=31, current_stamps=4, qr_code_token="stable-qr",
        )
        self.other_customer = Customer(
            business_id=self.other.id, full_name="Otro", phone="+525500000002",
        )
        admin = PlatformAdmin(name="Admin", email="publish@example.test", is_active=True)
        admin.set_password("SecurePassword123")
        db.session.add_all([self.customer, self.other_customer, admin]); db.session.commit()
        self.admin_id = admin.id
        with self.client.session_transaction() as browser:
            browser["platform_admin_id"] = admin.id
            browser["csrf_token"] = "publish-csrf"

    def tearDown(self):
        db.session.remove(); db.session.execute(text("PRAGMA foreign_keys=OFF"))
        db.drop_all(); self.ctx.pop()

    def enqueue(self, reason="Publicar diseño aprobado"):
        return self.client.post(
            f"/admin/business/{self.business.id}/wallet/publications",
            json={"confirm": True, "reason": reason},
            headers={"X-CSRFToken": "publish-csrf"},
        )

    def test_save_only_versions_design_and_never_enqueues(self):
        before = self.business.wallet_design_version
        response = self.client.post(
            f"/admin/business/{self.business.id}/update",
            json={"wallet_card_text": "Diseño guardado"},
            headers={"X-CSRFToken": "publish-csrf"},
        )
        self.assertEqual(response.status_code, 200)
        db.session.refresh(self.business)
        self.assertEqual(self.business.wallet_design_version, before + 1)
        self.assertEqual(GoogleWalletPublication.query.count(), 0)

    def test_explicit_publication_is_tenant_scoped_audited_and_idempotent(self):
        first = self.enqueue()
        second = self.enqueue()
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertFalse(first.get_json()["idempotent_replay"])
        self.assertTrue(second.get_json()["idempotent_replay"])
        publication = GoogleWalletPublication.query.one()
        self.assertEqual(publication.provider_mode, "mock")
        jobs = GoogleWalletPublicationJob.query.all()
        self.assertEqual([(job.business_id, job.customer_id) for job in jobs], [
            (self.business.id, self.customer.id),
        ])
        audit = AccessAudit.query.filter_by(action="wallet_publication_enqueued").one()
        self.assertEqual(audit.business_id, self.business.id)
        self.assertNotIn(self.customer.full_name, audit.new_state)

    def test_permissions_csrf_confirmation_and_disabled_mode_fail_closed(self):
        anonymous = self.app.test_client()
        with anonymous.session_transaction() as browser:
            browser["csrf_token"] = "anonymous-csrf"
        self.assertEqual(anonymous.post(
            f"/admin/business/{self.business.id}/wallet/publications", json={"confirm": True, "reason": "x"},
            headers={"X-CSRFToken": "anonymous-csrf"},
        ).status_code, 401)
        self.assertEqual(self.client.post(
            f"/admin/business/{self.business.id}/wallet/publications", json={"confirm": True, "reason": "x"},
        ).status_code, 400)
        self.assertEqual(self.client.post(
            f"/admin/business/{self.business.id}/wallet/publications", json={"reason": "x"},
            headers={"X-CSRFToken": "publish-csrf"},
        ).status_code, 400)
        self.app.config["GOOGLE_WALLET_MODE"] = "disabled"
        self.assertEqual(self.enqueue().status_code, 409)
        self.assertEqual(GoogleWalletPublication.query.count(), 0)

    def test_batch_processing_preserves_economic_identity_and_reports_mock(self):
        before = (self.customer.qr_code_token, self.customer.current_points, self.customer.current_stamps)
        self.assertEqual(self.enqueue().status_code, 202)
        publisher = MockPublisher()
        self.assertEqual(process_pending_wallet_publications(service=publisher), 1)
        publication = GoogleWalletPublication.query.one()
        job = GoogleWalletPublicationJob.query.one()
        self.assertEqual((publication.status, job.status), ("completed", "completed"))
        self.assertEqual(publication.provider_mode, "mock")
        db.session.refresh(self.customer)
        self.assertEqual((self.customer.qr_code_token, self.customer.current_points, self.customer.current_stamps), before)
        self.assertEqual((Transaction.query.count(), LoyaltyLedgerEntry.query.count()), (0, 0))

    def test_provider_mode_change_fails_closed_without_remote_call(self):
        self.enqueue()
        publisher = MockPublisher()
        publisher.mode = "real"
        self.assertEqual(process_pending_wallet_publications(service=publisher), 0)
        self.assertEqual(GoogleWalletPublication.query.one().status, "failed")
        self.assertEqual((publisher.class_calls, publisher.object_calls), ([], []))

    def test_customer_fence_prevents_design_balance_race(self):
        self.enqueue(); publisher = MockPublisher()
        with _customer_lock(self.customer.id) as acquired:
            self.assertTrue(acquired)
            self.assertEqual(process_pending_wallet_publications(service=publisher), 0)
        self.assertEqual(publisher.object_calls, [])
        self.assertEqual(process_pending_wallet_publications(service=publisher), 1)

    def test_transient_retry_and_new_design_supersede_old_work(self):
        self.enqueue()
        transient = MockPublisher(object_results=[{"status": "retry", "status_code": 503}])
        self.assertEqual(process_pending_wallet_publications(service=transient), 1)
        job = GoogleWalletPublicationJob.query.one()
        self.assertEqual(job.status, "pending")
        self.assertEqual(job.attempts, 1)
        self.business.wallet_design_version += 1; db.session.commit()
        self.assertEqual(process_pending_wallet_publications(service=MockPublisher()), 0)
        self.assertEqual(GoogleWalletPublication.query.one().status, "superseded")
        self.assertEqual(GoogleWalletPublicationJob.query.one().status, "superseded")

    def test_new_enqueue_supersedes_old_batch_and_jobs_immediately(self):
        self.enqueue()
        first = GoogleWalletPublication.query.one()
        self.business.wallet_design_version += 1; db.session.commit()
        self.assertEqual(self.enqueue("Versión nueva aprobada").status_code, 202)
        db.session.refresh(first)
        old_job = GoogleWalletPublicationJob.query.filter_by(publication_id=first.id).one()
        self.assertEqual((first.status, old_job.status), ("superseded", "superseded"))
        self.assertEqual(GoogleWalletPublication.query.count(), 2)

    def test_permanent_failure_can_be_retried_without_duplicate_jobs(self):
        self.enqueue()
        failure = MockPublisher(object_results=[{"status": "error", "status_code": 400}])
        self.assertEqual(process_pending_wallet_publications(service=failure), 1)
        publication = GoogleWalletPublication.query.one()
        self.assertEqual(publication.status, "failed")
        response = self.client.post(
            f"/admin/business/{self.business.id}/wallet/publications/{publication.id}/retry",
            headers={"X-CSRFToken": "publish-csrf"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(GoogleWalletPublicationJob.query.count(), 1)
        self.assertEqual(process_pending_wallet_publications(service=MockPublisher()), 1)
        self.assertEqual(GoogleWalletPublication.query.one().status, "completed")

    def test_cancel_is_tenant_scoped_audited_and_stops_pending_work(self):
        self.enqueue()
        publication = GoogleWalletPublication.query.one()
        cross_tenant = self.client.post(
            f"/admin/business/{self.other.id}/wallet/publications/{publication.id}/cancel",
            headers={"X-CSRFToken": "publish-csrf"},
        )
        self.assertEqual(cross_tenant.status_code, 404)
        cancelled = self.client.post(
            f"/admin/business/{self.business.id}/wallet/publications/{publication.id}/cancel",
            headers={"X-CSRFToken": "publish-csrf"},
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(GoogleWalletPublication.query.one().status, "cancelled")
        self.assertEqual(GoogleWalletPublicationJob.query.one().status, "cancelled")
        self.assertEqual(process_pending_wallet_publications(service=MockPublisher()), 0)
        self.assertEqual(AccessAudit.query.filter_by(action="wallet_publication_cancelled").count(), 1)

    def test_stale_worker_claim_is_recovered(self):
        self.enqueue()
        publication = GoogleWalletPublication.query.one()
        job = GoogleWalletPublicationJob.query.one()
        stale = _now() - timedelta(minutes=6)
        publication.status = "processing"; publication.locked_at = stale
        publication.class_published = True
        job.status = "processing"; job.locked_at = stale
        db.session.commit()
        self.assertEqual(process_pending_wallet_publications(service=MockPublisher()), 1)
        self.assertEqual(GoogleWalletPublication.query.one().status, "completed")


if __name__ == "__main__":
    unittest.main()
