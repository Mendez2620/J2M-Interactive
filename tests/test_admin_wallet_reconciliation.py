import unittest
from decimal import Decimal
from unittest.mock import patch

from app import create_app
from tests.admin_session_fixture import issue_for
from app.extensions import db
from app.models import AccessAudit, Business, PlatformAdmin
from app.services.google_wallet_reconciliation_plan import ReconciliationCandidate, ReconciliationPreview


class AdminWalletReconciliationTests(unittest.TestCase):
    def setUp(self):
        app = create_app("testing")
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
