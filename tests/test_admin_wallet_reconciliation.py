import unittest
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url

from app import create_app
from tests.admin_session_fixture import issue_for
from app.extensions import db
from app.models import AccessAudit, Business, Customer, PlatformAdmin, Transaction
from app.models.platform_admin_session import AdminWalletReview
from app.services.google_wallet_sync_service import run_google_wallet_sync_worker
from app.services.loyalty_transaction_service import process_loyalty_transaction
from app.services.google_wallet_reconciliation_plan import ReconciliationCandidate, ReconciliationPreview


class AdminWalletReconciliationTests(unittest.TestCase):
    def setUp(self):
        app = self.make_app()
        self.app = app
        context = app.app_context()
        context.push()
        self.addCleanup(context.pop)
        self.addCleanup(db.engine.dispose)
        self.addCleanup(db.session.remove)
        db.create_all()
        admin = PlatformAdmin(name="Synthetic Admin", email="admin@example.invalid",
                              password_hash="synthetic", is_active=True)
        admin.set_password("SyntheticPassword123")
        business = Business(name="Synthetic Wallet", slug="synthetic-wallet")
        db.session.add_all([admin, business])
        db.session.commit()
        self.admin_id = admin.id
        self.business_id = business.id
        self.path = f"/admin/business/{business.id}/wallet/reconciliation"
        self.client = app.test_client()

    def make_app(self):
        return create_app("testing")

    def authenticate(self):
        with self.client.session_transaction() as session:
            session["platform_admin_id"] = self.admin_id
            session["platform_admin_session"] = issue_for(session["platform_admin_id"])

    def test_anonymous_and_inactive_admin_are_denied(self):
        self.assertEqual(self.client.get(self.path).status_code, 302)
        admin = db.session.get(PlatformAdmin, self.admin_id)
        self.authenticate()
        admin.is_active = False
        db.session.commit()
        self.assertEqual(self.client.get(self.path).status_code, 302)
        admin.is_active = True
        db.session.commit()
        self.assertEqual(self.client.get(self.path).status_code, 302)

    def test_login_and_logout_reject_replay_of_signed_cookie(self):
        self.client.get("/admin/login")
        with self.client.session_transaction() as session:
            csrf = session["csrf_token"]
        response = self.client.post("/admin/login", data={
            "email": "admin@example.invalid", "password": "SyntheticPassword123",
            "csrf_token": csrf,
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(self.path).status_code, 200)
        copied_cookie = self.client.get_cookie("session").value
        with self.client.session_transaction() as session:
            csrf = session["csrf_token"]
        self.assertEqual(self.client.post("/admin/logout", data={"csrf_token": csrf}).status_code, 302)
        self.client.set_cookie("session", copied_cookie)
        self.assertEqual(self.client.get(self.path).status_code, 302)

    def test_active_admin_can_diagnose_without_constructing_provider(self):
        self.authenticate()
        with patch("app.services.google_wallet.GoogleWalletService.__init__",
                   side_effect=AssertionError("provider must remain unused")):
            response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("Synthetic Wallet", response.get_data(as_text=True))
        self.assertIn("No hay candidaturas", response.get_data(as_text=True))
        self.assertEqual(self.client.get(self.path + "?after=-1").status_code, 400)
        self.assertIn(self.client.post(self.path).status_code, (400, 405))

    def test_history_is_scoped_and_does_not_render_raw_audit_data(self):
        self.authenticate()
        db.session.add_all([
            AccessAudit(business_id=self.business_id, platform_admin_id=self.admin_id,
                        action="wallet_reconciliation_started", previous_state="private-request-key",
                        new_state='{"requested":2,"fingerprint":"private-fingerprint"}'),
            AccessAudit(business_id=self.business_id + 1, platform_admin_id=self.admin_id,
                        action="wallet_reconciliation_started", new_state='{"requested":17}'),
        ])
        db.session.commit()
        body = self.client.get(self.path).get_data(as_text=True)
        self.assertIn("Solicitados: 2", body)
        self.assertIn("Resultado inconcluso", body)
        for private in ("Solicitados: 17", "private-request-key", "private-fingerprint"):
            self.assertNotIn(private, body)

    def test_review_requires_csrf_and_is_bound_to_the_issuing_session(self):
        self.authenticate()
        target = self.path + "/reviews"
        self.assertEqual(self.client.post(target, data={"candidate": "42"}).status_code, 400)
        self.client.get(self.path)
        with self.client.session_transaction() as session:
            csrf = session["csrf_token"]
        candidate = ReconciliationCandidate(
            self.business_id, 42, 10, 20, None, Decimal("0"), 5, "stamps", 8, None,
        )
        preview = ReconciliationPreview(True, 1, {"eligible": 1}, (candidate,), None)
        with patch("app.services.google_wallet_reconciliation_plan.preview_google_wallet_reconciliation",
                   return_value=preview):
            invalid = self.client.post(target, data={"candidate": "43", "csrf_token": csrf})
            self.assertEqual(invalid.status_code, 409)
            response = self.client.post(target, data={"candidate": "42", "csrf_token": csrf})
        self.assertEqual(response.status_code, 303)
        self.assertEqual(self.client.get(response.location).status_code, 200)
        self.authenticate()
        self.assertEqual(self.client.get(response.location).status_code, 404)

    def queue_review(self, *, two=False):
        self.client.get("/admin/login")
        with self.client.session_transaction() as session:
            csrf = session["csrf_token"]
        self.assertEqual(self.client.post("/admin/login", data={
            "email": "admin@example.invalid", "password": "SyntheticPassword123", "csrf_token": csrf,
        }).status_code, 302)
        business = db.session.get(Business, self.business_id)
        business.loyalty_type = "stamps"
        business.stamps_reward_limit = 8
        customer = Customer(business_id=business.id, full_name="Synthetic customer")
        db.session.add(customer)
        db.session.commit()
        customer_id = customer.id
        self.app.config["GOOGLE_WALLET_MODE"] = "disabled"
        process_loyalty_transaction(
            business=business, customer_id=customer_id, staff_id=None, action="earn_stamp",
            tx_type="earn", points_amount=0, stamps_amount=1, purchase_amount=0,
            request_id="synthetic-review", fingerprint="synthetic-review",
        )
        ids = [str(customer_id)]
        if two:
            second = Customer(business_id=business.id, full_name="Second synthetic customer")
            db.session.add(second)
            db.session.commit()
            ids.append(str(second.id))
            process_loyalty_transaction(
                business=business, customer_id=second.id, staff_id=None, action="earn_stamp",
                tx_type="earn", points_amount=0, stamps_amount=1, purchase_amount=0,
                request_id="synthetic-review-second", fingerprint="synthetic-review-second",
            )
        db.session.remove()
        self.client.get(self.path)
        with self.client.session_transaction() as session:
            csrf = session["csrf_token"]
        review = self.client.post(self.path + "/reviews", data={"candidate": ids, "csrf_token": csrf})
        self.assertEqual(review.status_code, 303)
        confirmation = review.location + "/confirm"
        form = {"csrf_token": csrf, "confirm": "yes"}
        self.assertEqual(self.client.post(confirmation, data=form).status_code, 403)
        self.app.config.update(GOOGLE_WALLET_MODE="real", GOOGLE_WALLET_RECONCILIATION_ENABLED=True)
        for _ in range(2):
            self.assertEqual(self.client.post(confirmation, data=form).status_code, 303)
        return customer_id, review.location, form

    def test_confirmed_review_runs_once_through_existing_worker(self):
        customer_id, location, form = self.queue_review()
        calls = []

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(self, record, *, business_id):
                calls.append((record.id, business_id))
                return {"status": "success", "reason": "balance_updated"}

        run_google_wallet_sync_worker(service=Provider(), max_cycles=2, sleep_fn=lambda _seconds: None)
        self.assertEqual(calls, [(customer_id, self.business_id)])
        self.assertEqual(Transaction.query.count(), 1)
        self.assertEqual(AdminWalletReview.query.one().state, "finished")
        self.assertEqual(self.client.post(location + "/confirm", data=form).status_code, 303)
        self.assertIn("Confirmadas: 1", self.client.get(location).get_data(as_text=True))

    def run_with_wallet_transport(self, revoke_on_read):
        from app.models import GoogleWalletSyncJob, LoyaltyLedgerEntry
        from app.services.google_wallet import GoogleWalletService

        customer_id, location, form = self.queue_review()
        self.app.config["GOOGLE_ISSUER_ID"] = "123456"
        identifier = f"123456.business_{self.business_id}_customer_{customer_id}"
        calls = []
        browser = self.client

        class Response:
            status_code = 200
            content = b"synthetic"

            def json(self):
                return {
                    "id": identifier, "classId": identifier.split("_customer_")[0], "state": "active",
                    "loyaltyPoints": {"label": "Sellos", "balance": {
                        "string": "1 / 8" if calls[-1] == "PATCH" else "0 / 8",
                    }},
                }

        class Transport:
            def request(self, method, url, **kwargs):
                calls.append(method)
                if method == "GET" and revoke_on_read:
                    browser.post("/admin/logout", data={"csrf_token": form["csrf_token"]})
                return Response()

        service = GoogleWalletService(mode="real", http_session=Transport(), credentials_info={
            "type": "service_account", "project_id": "synthetic",
            "client_email": "wallet@synthetic.iam.gserviceaccount.com",
            "private_key": GoogleWalletService._generate_dev_private_key(),
            "token_uri": "https://example.invalid/token",
        })
        ledger_count = LoyaltyLedgerEntry.query.count()
        run_google_wallet_sync_worker(service=service, max_cycles=2, sleep_fn=lambda _seconds: None)
        self.assertEqual(calls, ["GET"] if revoke_on_read else ["GET", "PATCH"])
        self.assertEqual(GoogleWalletSyncJob.query.count(), 0 if revoke_on_read else 1)
        self.assertEqual(Transaction.query.count(), 1)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), ledger_count)
        if not revoke_on_read:
            self.assertIn("Confirmadas: 1", self.client.get(location).get_data(as_text=True))

    def test_real_wallet_transport_only_updates_existing_pass(self):
        self.run_with_wallet_transport(False)

    def test_logout_during_provider_get_prevents_patch(self):
        self.run_with_wallet_transport(True)

    def test_old_password_verification_cannot_issue_a_session(self):
        from app.services.platform_admin_session import issue

        admin = db.session.get(PlatformAdmin, self.admin_id)
        previously_verified = admin.password_hash
        admin.set_password("ChangedSyntheticPassword123")
        db.session.commit()
        self.assertIsNone(issue(admin, verified_password_hash=previously_verified))


@unittest.skipUnless(os.getenv("TEST_POSTGRES_URL"), "synthetic PostgreSQL not configured")
class PostgresAdminWalletReconciliationTests(AdminWalletReconciliationTests):
    def check_partial_batch(self, interruption):
        from datetime import timedelta
        from sqlalchemy import update
        from app.models import GoogleWalletSyncJob, LoyaltyLedgerEntry
        from app.models.platform_admin_session import PlatformAdminSession
        from app.services import admin_wallet_review, google_wallet_reconciliation_control as control
        from app.services.google_wallet import GoogleWalletService

        first, location, form = self.queue_review(two=True)
        review = AdminWalletReview.query.one()
        session_digest = review.session_digest
        boundary = admin_wallet_review.now()
        if interruption == "session":
            db.session.execute(update(PlatformAdminSession).where(
                PlatformAdminSession.token_digest == session_digest
            ).values(expires_at=boundary + timedelta(seconds=2)))
            db.session.commit()
        deadline = review.expires_at
        current = [boundary]
        self.app.config["GOOGLE_ISSUER_ID"] = "123456"
        target = f"123456.business_{self.business_id}_customer_{first}"
        calls = []

        class Response:
            status_code = 200
            content = b"synthetic"

            def json(self):
                return {"id": target, "classId": target.split("_customer_")[0], "state": "active",
                        "loyaltyPoints": {"label": "Sellos", "balance": {
                            "string": "1 / 8" if calls[-1][0] == "PATCH" else "0 / 8"}}}

        class Transport:
            def request(self, method, url, **kwargs):
                calls.append((method, url.rsplit("/", 1)[-1]))
                return Response()

        service = GoogleWalletService(mode="real", http_session=Transport(), credentials_info={
            "type": "service_account", "project_id": "synthetic",
            "client_email": "wallet@synthetic.iam.gserviceaccount.com",
            "private_key": GoogleWalletService._generate_dev_private_key(),
            "token_uri": "https://example.invalid/token",
        })
        original_append = control._append

        def interrupt(connection, action, **kwargs):
            original_append(connection, action, **kwargs)
            if action != control._ITEM_FINISH:
                return
            if interruption == "logout":
                self.assertEqual(self.client.post("/admin/logout", data={"csrf_token": form["csrf_token"]}).status_code, 302)
            else:
                current[0] = deadline if interruption == "review" else boundary + timedelta(seconds=3)

        ledger_count = LoyaltyLedgerEntry.query.count()
        with patch.object(admin_wallet_review, "now", side_effect=lambda: current[0]), \
             patch.object(control, "_append", side_effect=interrupt):
            run_google_wallet_sync_worker(service=service, max_cycles=2, sleep_fn=lambda _seconds: None)
        self.assertEqual(calls, [("GET", target), ("PATCH", target)])
        self.assertEqual(GoogleWalletSyncJob.query.one().customer_id, first)
        self.assertEqual(Transaction.query.count(), 2)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), ledger_count)
        persisted = AdminWalletReview.query.one()
        self.assertEqual(persisted.state, "finished")
        self.assertEqual(persisted.result["counts"], {"success": 1})
        self.assertEqual(persisted.result["remaining"], 1)
        self.assertEqual(persisted.result["stop_reason"], "authorization_expired")
        self.assertIn("authorization_expired", AccessAudit.query.filter_by(action=control._FINISH).one().new_state)
        if interruption != "logout":
            self.assertEqual(self.client.post(location + "/confirm", data=form).status_code, 303)
            run_google_wallet_sync_worker(service=service, max_cycles=1)
            self.assertEqual(calls, [("GET", target), ("PATCH", target)])
        self.authenticate()
        history = self.client.get(self.path).get_data(as_text=True)
        self.assertIn("Resultado parcial", history)
        self.assertIn("Autorización vencida o revocada", history)
        self.assertEqual(self.client.get(location).status_code, 404)
        persisted = AdminWalletReview.query.one()
        persisted.result = {**persisted.result, "remaining": 0}
        db.session.commit()
        corrupt_history = self.client.get(self.path).get_data(as_text=True)
        self.assertIn("Inconclusa", corrupt_history)
        self.assertNotIn("Resultado parcial", corrupt_history)

    def test_logout_after_first_customer_preserves_partial_batch(self):
        self.check_partial_batch("logout")

    def test_review_expiration_preserves_partial_batch(self):
        self.check_partial_batch("review")

    def test_session_expiration_preserves_partial_batch(self):
        self.check_partial_batch("session")

    def make_app(self):
        from app.config import TestingConfig

        url = make_url(os.environ["TEST_POSTGRES_URL"])
        if (url.get_backend_name() != "postgresql" or url.host not in {"localhost", "127.0.0.1"}
                or url.database != "j2m_test" or url.query):
            self.fail("Requires dedicated loopback synthetic PostgreSQL")
        url = url.set(drivername="postgresql+psycopg2")
        schema = "admin_review_" + uuid4().hex
        engine = create_engine(url)
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.exec_driver_sql(f"CREATE SCHEMA {schema}")

        def cleanup():
            with engine.begin() as connection:
                connection.exec_driver_sql(f"DROP SCHEMA {schema} CASCADE")

        self.addCleanup(cleanup)
        with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", url.render_as_string(hide_password=False)), \
             patch.object(TestingConfig, "SQLALCHEMY_ENGINE_OPTIONS", {
                 "connect_args": {"options": f"-csearch_path={schema}"},
             }):
            return create_app("testing")

    def test_concurrent_http_confirmation_and_worker_claim_do_not_duplicate_work(self):
        from app.services.admin_wallet_review import process_one_admin_wallet_review

        customer_id, location, form = self.queue_review()
        review = AdminWalletReview.query.one()
        review.state, review.confirmed_at = "prepared", None
        db.session.commit()
        cookie = self.client.get_cookie("session").value
        calls = []
        engine = db.engine

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(self, record, *, business_id):
                calls.append(record.id)
                return {"status": "success", "reason": "balance_updated"}

        def confirm_in_browser():
            client = self.app.test_client()
            client.set_cookie("session", cookie)
            return client.post(location + "/confirm", data=form).status_code

        def worker():
            with self.app.app_context():
                return process_one_admin_wallet_review(service=Provider())

        def compete(operation, expected_state):
            barrier = Barrier(2, timeout=10)

            def pause(_conn, _cursor, statement, parameters, _context, _many):
                if statement.startswith("UPDATE admin_wallet_reviews") and parameters.get("state") == expected_state:
                    barrier.wait()

            event.listen(engine, "before_cursor_execute", pause)
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    return [future.result(timeout=20) for future in
                            (executor.submit(operation), executor.submit(operation))]
            finally:
                event.remove(engine, "before_cursor_execute", pause)

        self.assertEqual(compete(confirm_in_browser, "queued"), [303, 303])
        db.session.expire_all()
        self.assertEqual(AdminWalletReview.query.one().state, "queued")
        db.session.rollback()
        self.assertEqual(sorted(compete(worker, "running")), [0, 1])
        self.assertEqual(calls, [customer_id])
        self.assertEqual(AdminWalletReview.query.one().state, "finished")
        self.assertEqual(Transaction.query.count(), 1)

    def test_connection_loss_before_audit_keeps_claim_without_replaying(self):
        from app.services import google_wallet_reconciliation_control as control
        from app.services.admin_wallet_review import process_one_admin_wallet_review

        self.queue_review()
        engine = db.engine
        killed = []

        class Provider:
            mode = "real"

            def reconcile_existing_loyalty_balance(self, record, *, business_id):
                raise AssertionError("No request can precede its audit")

        def interrupt(connection, action, **kwargs):
            pid, database, schema = connection.exec_driver_sql(
                "SELECT pg_backend_pid(), current_database(), current_schema()"
            ).one()
            self.assertEqual(database, "j2m_test")
            self.assertTrue(schema.startswith("admin_review_"))
            with engine.begin() as independent:
                killed.append(independent.exec_driver_sql(
                    "SELECT pg_terminate_backend(%s, 5000)", (pid,)
                ).scalar_one())
            connection.exec_driver_sql("SELECT 1")

        with patch.object(control, "_append", side_effect=interrupt):
            self.assertEqual(process_one_admin_wallet_review(service=Provider()), 1)
        self.assertEqual(killed, [True])
        self.assertEqual(process_one_admin_wallet_review(service=Provider()), 0)
        review = AdminWalletReview.query.one()
        self.assertEqual(review.state, "inconclusive")
        self.assertTrue(review.owner)
        self.assertEqual(AccessAudit.query.count(), 0)
        self.assertIn("Inconclusa", self.client.get(self.path).get_data(as_text=True))
