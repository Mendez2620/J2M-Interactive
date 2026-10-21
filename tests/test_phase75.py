import re
import unittest
from datetime import timedelta

from app import create_app
from app.extensions import db
from app.models import (
    Business, Customer, GoogleWalletSyncJob, LoyaltyLedgerEntry, PlatformAdmin,
    Staff, StaffSession, Transaction,
)
from app.services.google_wallet_sync_service import (
    _now, google_wallet_sync_metrics, run_google_wallet_sync_worker,
)
from app.services.loyalty_transaction_service import process_loyalty_transaction


class SuccessfulWalletService:
    mode = "real"

    def __init__(self):
        self.balances = []

    def update_loyalty_object(self, customer):
        self.balances.append((customer.business_id, customer.id, customer.current_stamps))
        return {"status": "success", "status_code": 200}


class RetryingWalletService:
    mode = "real"

    def update_loyalty_object(self, _customer):
        return {"status": "retry", "status_code": 429}


class Phase75TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(
            CSRF_ENABLED=True,
            SESSION_COOKIE_SECURE=True,
            GOOGLE_WALLET_MODE="real",
        )
        self.client = self.app.test_client()
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.business = Business(
            name="Wallet estable", slug="wallet-estable", loyalty_type="stamps",
            stamps_reward_limit=8, cooldown_minutes=0, is_active=True,
            web_access_enabled=True, mobile_access_enabled=True,
        )
        self.other_business = Business(
            name="Otro tenant", slug="otro-tenant", loyalty_type="stamps",
            stamps_reward_limit=8, cooldown_minutes=0, is_active=True,
            web_access_enabled=True, mobile_access_enabled=True,
        )
        db.session.add_all([self.business, self.other_business])
        db.session.flush()
        self.cashier = Staff(
            business_id=self.business.id, username="caja75", role="cashier",
            permissions="scanner_operate_web", is_active=True,
            web_access_enabled=True, mobile_access_enabled=True,
        )
        self.cashier.set_password("PasswordSeguro123")
        self.manager = Staff(
            business_id=self.business.id, username="gerente75", email="gerente75@example.test",
            role="manager", permissions="view_stats,scanner_operate_web", is_active=True,
            web_access_enabled=True, mobile_access_enabled=True,
        )
        self.manager.set_password("PasswordSeguro123")
        self.customer = Customer(
            business_id=self.business.id, full_name="Cliente Wallet", current_stamps=0,
        )
        self.other_customer = Customer(
            business_id=self.other_business.id, full_name="Cliente aislado", current_stamps=0,
        )
        self.admin = PlatformAdmin(name="Admin 75", email="admin75@example.test", is_active=True)
        self.admin.set_password("PasswordSeguro123")
        db.session.add_all([self.cashier, self.manager, self.customer, self.other_customer, self.admin])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    @staticmethod
    def csrf(html):
        match = re.search(r'name="csrf-token" content="([^"]+)"', html)
        if not match:
            match = re.search(r'name="csrf_token" value="([^"]+)"', html)
        return match.group(1) if match else None

    def https_get(self, path, **kwargs):
        return self.client.get(path, base_url="https://localhost", **kwargs)

    def https_post(self, path, **kwargs):
        return self.client.post(path, base_url="https://localhost", **kwargs)

    def test_staff_csrf_rotates_with_login_and_supports_scanner_logout_and_relogin(self):
        login_page = self.https_get("/staff/login")
        anonymous_token = self.csrf(login_page.get_data(as_text=True))
        login = self.https_post("/staff/login", data={
            "identity": "caja75", "business_slug": self.business.slug,
            "password": "PasswordSeguro123", "csrf_token": anonymous_token,
        })
        self.assertEqual(login.status_code, 302)
        scanner = self.https_get(login.location)
        authenticated_token = self.csrf(scanner.get_data(as_text=True))
        self.assertTrue(authenticated_token)
        self.assertNotEqual(anonymous_token, authenticated_token)
        lookup = self.https_post(
            "/api/web-scanner/lookup", json={"token": self.customer.qr_code_token},
            headers={"X-CSRFToken": authenticated_token},
        )
        self.assertEqual(lookup.status_code, 200)
        movement = self.https_post(
            "/api/web-scanner/process",
            json={"token": self.customer.qr_code_token, "action": "earn_stamp"},
            headers={"X-CSRFToken": authenticated_token, "Idempotency-Key": "phase75-csrf-flow"},
        )
        self.assertEqual(movement.status_code, 200)
        self.assertEqual(db.session.get(Customer, self.customer.id).current_stamps, 1)
        logout = self.https_post("/staff/logout", data={"csrf_token": authenticated_token})
        self.assertEqual(logout.status_code, 302)
        self.assertTrue(StaffSession.query.filter_by(staff_id=self.cashier.id).one().is_revoked)
        relogin_page = self.https_get("/staff/login")
        relogin_token = self.csrf(relogin_page.get_data(as_text=True))
        relogin = self.https_post("/staff/login", data={
            "identity": "caja75", "business_slug": self.business.slug,
            "password": "PasswordSeguro123", "csrf_token": relogin_token,
        })
        self.assertEqual(relogin.status_code, 302)
        self.assertEqual(self.https_get("/staff/portal/scanner").status_code, 200)

    def test_manager_csrf_and_stale_html_logout_fail_safely(self):
        page = self.https_get("/manager/login")
        token = self.csrf(page.get_data(as_text=True))
        login = self.https_post("/manager/login", data={
            "email": self.manager.email, "password": "PasswordSeguro123", "csrf_token": token,
        })
        self.assertEqual(login.status_code, 302)
        portal = self.https_get(login.location)
        current_token = self.csrf(portal.get_data(as_text=True))
        self.assertTrue(current_token)
        stale = self.https_post(
            "/manager/logout", data={"csrf_token": token},
            headers={"Accept": "text/html"},
        )
        self.assertEqual(stale.status_code, 303)
        self.assertIn("csrf_error=1", stale.location)
        self.assertEqual(self.https_get("/manager/portal").status_code, 200)
        self.assertEqual(self.https_post("/manager/logout", data={"csrf_token": current_token}).status_code, 302)

    def create_stamp(self, customer, request_id):
        return process_loyalty_transaction(
            business=customer.business, customer_id=customer.id, staff_id=self.cashier.id,
            action="earn_stamp", tx_type="earn", points_amount=0, stamps_amount=1,
            purchase_amount=None, request_id=request_id, fingerprint=f"fp:{request_id}",
        )

    def test_worker_completes_zero_to_one_flow_once_and_uses_current_balance(self):
        first = self.create_stamp(self.customer, "phase75-stamp")
        replay = self.create_stamp(self.customer, "phase75-stamp")
        self.assertFalse(first.replay)
        self.assertTrue(replay.replay)
        service = SuccessfulWalletService()
        run_google_wallet_sync_worker(service=service, max_cycles=1, poll_seconds=1)
        self.assertEqual(service.balances, [(self.business.id, self.customer.id, 1)])
        self.assertEqual(Transaction.query.filter_by(customer_id=self.customer.id).count(), 1)
        self.assertEqual(LoyaltyLedgerEntry.query.filter(
            LoyaltyLedgerEntry.customer_id == self.customer.id,
            LoyaltyLedgerEntry.transaction_id.isnot(None),
        ).count(), 1)
        job = GoogleWalletSyncJob.query.one()
        self.assertEqual(job.status, "completed")
        self.assertEqual(google_wallet_sync_metrics()["completed"], 1)

    def test_worker_coalesces_backlog_isolates_tenants_and_recovers_stale_jobs(self):
        self.create_stamp(self.customer, "phase75-a")
        self.create_stamp(self.customer, "phase75-b")
        other_staff = Staff(
            business_id=self.other_business.id, username="other75", role="cashier",
            permissions="scanner_operate_web", is_active=True,
            web_access_enabled=True, mobile_access_enabled=True,
        )
        other_staff.set_password("PasswordSeguro123")
        db.session.add(other_staff)
        db.session.commit()
        process_loyalty_transaction(
            business=self.other_business, customer_id=self.other_customer.id,
            staff_id=other_staff.id, action="earn_stamp", tx_type="earn",
            points_amount=0, stamps_amount=1, purchase_amount=None,
            request_id="phase75-other", fingerprint="fp:phase75-other",
        )
        stale = GoogleWalletSyncJob.query.filter_by(customer_id=self.customer.id).first()
        stale.status = "processing"
        stale.locked_at = _now() - timedelta(minutes=10)
        db.session.commit()
        service = SuccessfulWalletService()
        run_google_wallet_sync_worker(service=service, max_cycles=1)
        self.assertCountEqual(service.balances, [
            (self.business.id, self.customer.id, 2),
            (self.other_business.id, self.other_customer.id, 1),
        ])
        self.assertEqual(GoogleWalletSyncJob.query.filter_by(status="completed").count(), 3)

    def test_retry_is_persistent_and_admin_exposes_only_aggregate_state(self):
        self.create_stamp(self.customer, "phase75-retry")
        run_google_wallet_sync_worker(service=RetryingWalletService(), max_cycles=1)
        job = GoogleWalletSyncJob.query.one()
        self.assertEqual((job.status, job.attempts, job.last_status_code), ("pending", 1, 429))
        self.assertIsNotNone(job.next_attempt_at)
        with self.client.session_transaction() as session:
            session["platform_admin_id"] = self.admin.id
        dashboard = self.https_get("/admin").get_data(as_text=True)
        self.assertIn("1 reintentando", dashboard)
        self.assertIn("Último intento", dashboard)
        self.assertNotIn(self.customer.full_name, dashboard)
        self.assertNotIn(self.customer.qr_code_token, dashboard)


if __name__ == "__main__":
    unittest.main()
