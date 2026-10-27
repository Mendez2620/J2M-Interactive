import base64
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import tempfile
from pathlib import Path
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

import jwt

from app import create_app
from app.extensions import db
from app.models import Business, Customer, PlatformAdmin, Staff, Transaction
from app.wallet_access import issue_wallet_token
from app.config import TestingConfig


class SecurityPhase1TestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.business_a = Business(name="A", slug="a", loyalty_type="stamps", stamps_reward_limit=3,
                                   cooldown_minutes=0, is_active=True, web_access_enabled=True,
                                   mobile_access_enabled=True)
        self.business_b = Business(name="B", slug="b", loyalty_type="points", points_per_currency=2,
                                   cooldown_minutes=0, is_active=True, web_access_enabled=True,
                                   mobile_access_enabled=True)
        db.session.add_all([self.business_a, self.business_b])
        db.session.flush()
        self.cashier = self._staff(self.business_a, "shared", "cashier", "scanner_operate")
        self.other_cashier = self._staff(self.business_b, "shared", "cashier", "scanner_operate")
        self.manager = self._staff(self.business_a, "manager", "manager", "view_stats,view_transactions")
        self.customer_a = Customer(business_id=self.business_a.id, full_name="Cliente A", email="a@test.com",
                                   phone="111", current_stamps=2)
        self.customer_a2 = Customer(business_id=self.business_a.id, full_name="Cliente A2", email="a2@test.com",
                                    phone="112", current_stamps=0)
        self.customer_b = Customer(business_id=self.business_b.id, full_name="Cliente B", email="b@test.com",
                                   phone="222", current_points=100)
        admin = PlatformAdmin(name="Admin", email="admin@test.com", is_active=True)
        admin.set_password("AdminPassword123!")
        db.session.add_all([self.customer_a, self.customer_a2, self.customer_b, admin])
        db.session.commit()
        with self.client.session_transaction() as session:
            session["platform_admin_id"] = admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def _staff(self, business, username, role, permissions):
        staff = Staff(business_id=business.id, username=username, role=role, is_active=True,
                      web_access_enabled=True, mobile_access_enabled=True, permissions=permissions)
        staff.set_password("StaffPassword123!")
        db.session.add(staff)
        return staff

    def _login(self, staff, channel="mobile", **changes):
        payload = {"username": staff.username, "password": "StaffPassword123!",
                   "business_slug": staff.business.slug, "channel": channel}
        payload.update(changes)
        return self.client.post("/api/scanner/login", json=payload)

    def _auth(self, staff=None, channel="mobile"):
        response = self._login(staff or self.cashier, channel=channel)
        self.assertEqual(response.status_code, 200)
        return {"Authorization": f"Bearer {response.get_json()['access_token']}"}, response.get_json()

    def test_login_real_credentials_and_duplicate_username_resolution(self):
        self.assertEqual(self._login(self.cashier, password="wrong").status_code, 401)
        ambiguous = self.client.post("/api/scanner/login", json={
            "username": "shared", "password": "StaffPassword123!"})
        self.assertEqual(ambiguous.status_code, 400)
        self.assertEqual(self._login(self.cashier).status_code, 200)

    def test_forged_wrong_signature_expired_issuer_and_audience_tokens(self):
        headers, tokens = self._auth()
        access = tokens["access_token"]
        payload = jwt.decode(access, options={"verify_signature": False})
        forged = jwt.encode(payload, "wrong-signing-key-that-is-long-enough-123", algorithm="HS256")
        self.assertEqual(self.client.post("/api/scanner/lookup", json={"token": self.customer_a.qr_code_token},
                                          headers={"Authorization": f"Bearer {forged}"}).status_code, 401)
        for field, value in (("iss", "evil"), ("aud", "evil")):
            changed = dict(payload)
            changed[field] = value
            token = jwt.encode(changed, self.app.config["JWT_SECRET_KEY"], algorithm="HS256")
            response = self.client.post("/api/scanner/lookup", json={"token": self.customer_a.qr_code_token},
                                        headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, 401)
        expired = dict(payload)
        expired["exp"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        token = jwt.encode(expired, self.app.config["JWT_SECRET_KEY"], algorithm="HS256")
        self.assertEqual(self.client.post("/api/scanner/lookup", json={"token": self.customer_a.qr_code_token},
                                          headers={"Authorization": f"Bearer {token}"}).status_code, 401)

    def test_logout_and_password_reset_revoke_existing_tokens(self):
        headers, _ = self._auth()
        self.assertEqual(self.client.post("/api/scanner/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/scanner/lookup", json={"token": self.customer_a.qr_code_token},
                                          headers=headers).status_code, 401)
        headers, _ = self._auth()
        response = self.client.post(f"/admin/staff/{self.cashier.id}/reset-password", json={
            "password": "NewStaffPassword123!", "password_confirmation": "NewStaffPassword123!"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.post("/api/scanner/lookup", json={"token": self.customer_a.qr_code_token},
                                          headers=headers).status_code, 401)

    def test_refresh_rotation_rejects_reuse(self):
        _, tokens = self._auth()
        first = self.client.post("/api/scanner/refresh", json={"refresh_token": tokens["refresh_token"]})
        self.assertEqual(first.status_code, 200)
        reuse = self.client.post("/api/scanner/refresh", json={"refresh_token": tokens["refresh_token"]})
        self.assertEqual(reuse.status_code, 401)

    def test_concurrent_refresh_only_one_consumes_token(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            uri = f"sqlite:///{(Path(directory) / 'refresh.sqlite').as_posix()}"
            with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
                concurrent_app = create_app("testing")
            with concurrent_app.app_context():
                db.create_all()
                business = Business(name="Concurrent", slug="concurrent", is_active=True,
                                    mobile_access_enabled=True)
                db.session.add(business)
                db.session.flush()
                staff = Staff(business_id=business.id, username="concurrent", role="cashier",
                              is_active=True, mobile_access_enabled=True, permissions="scanner_operate")
                staff.set_password("StaffPassword123!")
                db.session.add(staff)
                db.session.commit()
            login_client = concurrent_app.test_client()
            login = login_client.post("/api/scanner/login", json={"username": "concurrent",
                "password": "StaffPassword123!", "business_slug": "concurrent"})
            refresh_token = login.get_json()["refresh_token"]
            barrier = Barrier(2)

            def rotate():
                with concurrent_app.test_client() as client:
                    barrier.wait(timeout=5)
                    return client.post("/api/scanner/refresh", json={
                        "refresh_token": refresh_token}).status_code

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = sorted(executor.map(lambda _index: rotate(), range(2)))
            self.assertEqual(statuses, [200, 401])

    def test_account_business_and_channel_changes_are_checked_each_request(self):
        headers, _ = self._auth()
        self.cashier.is_active = False
        db.session.commit()
        self.assertEqual(self.client.post("/api/scanner/lookup", json={"token": self.customer_a.qr_code_token}, headers=headers).status_code, 403)
        self.cashier.is_active = True
        db.session.commit()
        headers, _ = self._auth()
        self.business_a.is_active = False
        db.session.commit()
        self.assertEqual(self.client.post("/api/scanner/lookup", json={"token": self.customer_a.qr_code_token}, headers=headers).status_code, 403)
        self.business_a.is_active = True
        self.business_a.mobile_access_enabled = False
        db.session.commit()
        self.assertEqual(self.client.post("/api/scanner/lookup", json={"token": self.customer_a.qr_code_token}, headers=headers).status_code, 403)

    def test_anonymous_scanner_requests_do_not_modify_data(self):
        before = self.customer_a.current_stamps
        self.assertEqual(self.client.post("/api/scanner/lookup", json={"token": self.customer_a.qr_code_token}).status_code, 401)
        response = self.client.post("/api/scanner/process", json={"token": self.customer_a.qr_code_token,
            "action": "earn_stamp", "request_id": "anonymous"})
        self.assertEqual(response.status_code, 401)
        db.session.refresh(self.customer_a)
        self.assertEqual(self.customer_a.current_stamps, before)
        self.assertEqual(Transaction.query.count(), 0)

    def test_tenant_isolation_and_client_identity_fields_are_ignored(self):
        headers, _ = self._auth()
        response = self.client.post("/api/scanner/lookup", json={"token": self.customer_b.qr_code_token,
            "business_id": self.business_b.id}, headers=headers)
        self.assertEqual(response.status_code, 404)
        response = self.client.post("/api/scanner/process", json={"token": self.customer_a.qr_code_token,
            "business_id": self.business_b.id, "staff_id": self.other_cashier.id,
            "action": "earn_stamp", "request_id": "tenant-safe"}, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["transaction"]["staff_id"], self.cashier.id)

    def test_invalid_amounts_and_wrong_program_leave_balances_unchanged(self):
        headers, _ = self._auth()
        before = self.customer_a.current_stamps
        for index, amount in enumerate((float("nan"), float("inf"), -1, 1000001)):
            response = self.client.post("/api/scanner/process", json={"token": self.customer_a.qr_code_token,
                "action": "earn_stamp", "purchase_amount": amount, "request_id": f"bad-{index}"}, headers=headers)
            self.assertEqual(response.status_code, 400)
        wrong = self.client.post("/api/scanner/process", json={"token": self.customer_a.qr_code_token,
            "action": "earn_points", "purchase_amount": 10, "request_id": "wrong-program"}, headers=headers)
        self.assertEqual(wrong.status_code, 400)
        db.session.refresh(self.customer_a)
        self.assertEqual(self.customer_a.current_stamps, before)
        self.assertEqual(Transaction.query.count(), 0)

    def test_idempotency_prevents_double_application(self):
        headers, _ = self._auth()
        payload = {"token": self.customer_a.qr_code_token, "action": "earn_stamp", "request_id": "same-request"}
        self.assertEqual(self.client.post("/api/scanner/process", json=payload, headers=headers).status_code, 200)
        replay = self.client.post("/api/scanner/process", json=payload, headers=headers)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.get_json()["idempotent_replay"])
        db.session.refresh(self.customer_a)
        self.assertEqual(self.customer_a.current_stamps, 3)
        self.assertEqual(Transaction.query.count(), 1)

        changed_customer = self.client.post("/api/scanner/process", json={
            "token": self.customer_a2.qr_code_token, "action": "earn_stamp",
            "request_id": "same-request"}, headers=headers)
        self.assertEqual(changed_customer.status_code, 409)
        changed_action = self.client.post("/api/scanner/process", json={
            "token": self.customer_a.qr_code_token, "action": "redeem_stamp", "amount": 3,
            "request_id": "same-request"}, headers=headers)
        self.assertEqual(changed_action.status_code, 409)
        db.session.refresh(self.customer_a)
        db.session.refresh(self.customer_a2)
        self.assertEqual(self.customer_a.current_stamps, 3)
        self.assertEqual(self.customer_a2.current_stamps, 0)
        self.assertEqual(Transaction.query.count(), 1)

        other_headers, _ = self._auth(self.other_cashier)
        collision = self.client.post("/api/scanner/process", json={"token": self.customer_b.qr_code_token,
            "action": "earn_points", "purchase_amount": 10, "request_id": "same-request"}, headers=other_headers)
        self.assertEqual(collision.status_code, 200)
        self.assertEqual(Transaction.query.count(), 2)
        changed_amount = self.client.post("/api/scanner/process", json={"token": self.customer_b.qr_code_token,
            "action": "earn_points", "purchase_amount": 11, "request_id": "same-request"}, headers=other_headers)
        self.assertEqual(changed_amount.status_code, 409)
        db.session.refresh(self.customer_b)
        self.assertEqual(self.customer_b.current_points, 120)
        self.assertEqual(Transaction.query.count(), 2)

    def test_manager_role_and_tenant_authorization(self):
        cashier_headers, _ = self._auth(self.cashier, channel="web")
        self.assertEqual(self.client.get("/manager/dashboard", headers=cashier_headers).status_code, 403)
        manager_headers, _ = self._auth(self.manager, channel="web")
        self.assertEqual(self.client.get("/manager/dashboard?staff_id=999", headers=manager_headers).status_code, 200)
        self.assertEqual(self.client.get(f"/manager/{self.business_b.id}/transactions", headers=manager_headers).status_code, 403)
        self.assertEqual(self.client.get(f"/manager/business/{self.business_a.id}/export/pdf", headers=manager_headers).status_code, 403)
        self.assertEqual(self.client.post("/api/scanner/logout", headers=manager_headers).status_code, 200)

    def test_existing_customer_cannot_be_claimed_or_changed(self):
        response = self.client.post("/join/a", json={"full_name": "Atacante", "email": "a@test.com",
                                                       "phone_country": "+52", "phone": "55 1111 1111",
                                                       "birthdate": "1990-01-01", "terms_accepted": True,
                                                       "privacy_accepted": True})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["status"], "success")
        db.session.refresh(self.customer_a)
        self.assertEqual(self.customer_a.full_name, "Cliente A")
        self.assertEqual(self.customer_a.phone, "111")

    def test_wallet_requires_customer_scoped_temporary_token(self):
        self.assertEqual(self.client.get(f"/api/customers/{self.customer_a.id}/wallet/google").status_code, 403)
        token = issue_wallet_token(self.customer_a)
        ciphertext = base64.urlsafe_b64decode(token.encode("ascii"))
        self.assertNotIn(b'"customer_id"', ciphertext)
        self.assertNotIn(b'"business_id"', ciphertext)
        self.assertEqual(self.client.get(f"/api/customers/{self.customer_b.id}/wallet/google",
                                         query_string={"token": token}).status_code, 403)
        tampered = f"{token[:-1]}{'A' if token[-1] != 'A' else 'B'}"
        self.assertEqual(self.client.get(f"/api/customers/{self.customer_a.id}/wallet/apple",
                                         query_string={"token": tampered}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
