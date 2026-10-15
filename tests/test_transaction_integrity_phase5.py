import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from app import create_app
from app.config import TestingConfig
from app.extensions import db
from app.models import Business, Customer, LoyaltyLedgerEntry, Staff, Transaction
from app.services.loyalty_transaction_service import reconcile_customer_balance


class TransactionIntegrityPhase5TestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        uri = f"sqlite:///{(Path(self.directory.name) / 'integrity.sqlite').as_posix()}"
        with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", uri):
            self.app = create_app("testing")
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        self.points_business = Business(
            name="Puntos", slug="puntos", loyalty_type="points", points_per_currency=1,
            cooldown_minutes=0, is_active=True, mobile_access_enabled=True,
        )
        self.stamps_business = Business(
            name="Sellos", slug="sellos", loyalty_type="stamps", stamps_reward_limit=5,
            cooldown_minutes=0, is_active=True, mobile_access_enabled=True,
        )
        db.session.add_all([self.points_business, self.stamps_business]); db.session.flush()
        self.points_staff = self._staff(self.points_business, "points-cashier")
        self.stamps_staff = self._staff(self.stamps_business, "stamps-cashier")
        self.points_customer = Customer(
            business_id=self.points_business.id, full_name="Puntos Cliente", current_points=100,
        )
        self.stamps_customer = Customer(
            business_id=self.stamps_business.id, full_name="Sellos Cliente", current_stamps=10,
        )
        db.session.add_all([self.points_customer, self.stamps_customer]); db.session.commit()
        self.points_customer_id = self.points_customer.id
        self.stamps_customer_id = self.stamps_customer.id
        self.points_token = self.points_customer.qr_code_token
        self.stamps_token = self.stamps_customer.qr_code_token
        self.stamps_business_id = self.stamps_business.id
        self.points_headers = self._login("points-cashier", "puntos")
        self.stamps_headers = self._login("stamps-cashier", "sellos")
        db.session.remove()

    def tearDown(self):
        db.session.remove(); db.drop_all(); db.engine.dispose(); self.ctx.pop()
        self.directory.cleanup()

    def _staff(self, business, username):
        staff = Staff(
            business_id=business.id, username=username, role="cashier", is_active=True,
            mobile_access_enabled=True, permissions="scanner_operate",
        )
        staff.set_password("StaffPassword123!"); db.session.add(staff)
        return staff

    def _login(self, username, slug):
        response = self.app.test_client().post("/api/scanner/login", json={
            "username": username, "password": "StaffPassword123!", "business_slug": slug,
        })
        self.assertEqual(response.status_code, 200)
        return {"Authorization": f"Bearer {response.get_json()['access_token']}"}

    def _request(self, payload, headers, barrier=None):
        with self.app.test_client() as client:
            if barrier:
                barrier.wait(timeout=10)
            response = client.post("/api/scanner/process", json=payload, headers=headers)
            return response.status_code, response.get_json()

    def _parallel(self, requests):
        barrier = Barrier(len(requests))
        with ThreadPoolExecutor(max_workers=len(requests)) as executor:
            futures = [executor.submit(self._request, payload, headers, barrier) for payload, headers in requests]
            return [future.result(timeout=20) for future in futures]

    def test_simultaneous_redemptions_cannot_double_spend(self):
        base = {"token": self.points_token, "action": "redeem_points", "amount": 80}
        results = self._parallel([
            ({**base, "request_id": "redeem-a"}, self.points_headers),
            ({**base, "request_id": "redeem-b"}, self.points_headers),
        ])
        self.assertEqual(sorted(status for status, _ in results), [200, 400])
        customer = db.session.get(Customer, self.points_customer_id)
        self.assertEqual(customer.current_points, 20)
        self.assertEqual(Transaction.query.filter_by(customer_id=customer.id).count(), 1)
        self.assertEqual(LoyaltyLedgerEntry.query.filter(LoyaltyLedgerEntry.transaction_id.isnot(None)).count(), 1)
        self.assertTrue(reconcile_customer_balance(customer)["consistent"])

    def test_point_redemption_rejects_values_rounded_to_zero(self):
        initial = db.session.get(Customer, self.points_customer_id).current_points
        for index, amount in enumerate((0.0000001, 0.0000004)):
            status, _ = self._request({
                "token": self.points_token, "action": "redeem_points",
                "amount": amount, "request_id": f"tiny-{index}",
            }, self.points_headers)
            self.assertEqual(status, 400)

        customer = db.session.get(Customer, self.points_customer_id)
        db.session.refresh(customer)
        self.assertEqual(customer.current_points, initial)
        self.assertEqual(Transaction.query.count(), 0)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), 0)

        payload = {
            "token": self.points_token, "action": "redeem_points",
            "amount": 0.000001, "request_id": "minimum-point-redemption",
        }
        self.assertEqual(self._request(payload, self.points_headers)[0], 200)
        replay_status, replay = self._request(payload, self.points_headers)
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["idempotent_replay"])
        customer = db.session.get(Customer, self.points_customer_id)
        db.session.refresh(customer)
        self.assertAlmostEqual(customer.current_points, initial - 0.000001, places=6)
        self.assertEqual(Transaction.query.count(), 1)
        self.assertEqual(LoyaltyLedgerEntry.query.filter(LoyaltyLedgerEntry.transaction_id.isnot(None)).count(), 1)
        self.assertTrue(reconcile_customer_balance(customer)["consistent"])

    def test_simultaneous_stamp_earnings_are_not_lost(self):
        base = {"token": self.stamps_token, "action": "earn_stamp"}
        results = self._parallel([
            ({**base, "request_id": "earn-a"}, self.stamps_headers),
            ({**base, "request_id": "earn-b"}, self.stamps_headers),
        ])
        self.assertEqual([status for status, _ in results], [200, 200])
        customer = db.session.get(Customer, self.stamps_customer_id)
        self.assertEqual(customer.current_stamps, 12)
        self.assertEqual(Transaction.query.filter_by(customer_id=customer.id).count(), 2)
        self.assertEqual(LoyaltyLedgerEntry.query.filter(LoyaltyLedgerEntry.transaction_id.isnot(None)).count(), 2)
        self.assertTrue(reconcile_customer_balance(customer)["consistent"])

    def test_simultaneous_earn_and_redeem_have_serial_ledger(self):
        results = self._parallel([
            ({"token": self.points_token, "action": "earn_points",
              "purchase_amount": 20, "request_id": "mixed-earn"}, self.points_headers),
            ({"token": self.points_token, "action": "redeem_points",
              "amount": 80, "request_id": "mixed-redeem"}, self.points_headers),
        ])
        self.assertEqual([status for status, _ in results], [200, 200])
        customer = db.session.get(Customer, self.points_customer_id)
        self.assertEqual(customer.current_points, 40)
        self.assertTrue(reconcile_customer_balance(customer)["consistent"])

    def test_concurrent_identical_idempotency_key_applies_once(self):
        payload = {"token": self.stamps_token, "action": "earn_stamp", "request_id": "same"}
        results = self._parallel([(payload, self.stamps_headers), (payload, self.stamps_headers)])
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(sum(bool(body.get("idempotent_replay")) for _, body in results), 1)
        customer = db.session.get(Customer, self.stamps_customer_id)
        self.assertEqual(customer.current_stamps, 11)
        self.assertEqual(Transaction.query.filter_by(customer_id=customer.id).count(), 1)
        self.assertEqual(LoyaltyLedgerEntry.query.filter(LoyaltyLedgerEntry.transaction_id.isnot(None)).count(), 1)

    def test_concurrent_changed_idempotency_key_conflicts(self):
        requests = [
            ({"token": self.points_token, "action": "earn_points",
              "purchase_amount": amount, "request_id": "changed"}, self.points_headers)
            for amount in (10, 20)
        ]
        results = self._parallel(requests)
        self.assertEqual(sorted(status for status, _ in results), [200, 409])
        self.assertEqual(Transaction.query.filter_by(customer_id=self.points_customer_id).count(), 1)
        self.assertEqual(LoyaltyLedgerEntry.query.filter(LoyaltyLedgerEntry.transaction_id.isnot(None)).count(), 1)

    def test_same_key_in_two_businesses_remains_isolated(self):
        results = self._parallel([
            ({"token": self.points_token, "action": "earn_points",
              "purchase_amount": 5, "request_id": "tenant-key"}, self.points_headers),
            ({"token": self.stamps_token, "action": "earn_stamp",
              "request_id": "tenant-key"}, self.stamps_headers),
        ])
        self.assertEqual([status for status, _ in results], [200, 200])
        self.assertEqual(Transaction.query.filter_by(request_id="tenant-key").count(), 2)

    def test_concurrent_cooldown_allows_only_one_earning(self):
        db.session.get(Business, self.stamps_business_id).cooldown_minutes = 60; db.session.commit(); db.session.remove()
        base = {"token": self.stamps_token, "action": "earn_stamp"}
        results = self._parallel([
            ({**base, "request_id": "cooldown-a"}, self.stamps_headers),
            ({**base, "request_id": "cooldown-b"}, self.stamps_headers),
        ])
        self.assertEqual(sorted(status for status, _ in results), [200, 429])
        customer = db.session.get(Customer, self.stamps_customer_id)
        self.assertEqual(customer.current_stamps, 11)
        self.assertEqual(Transaction.query.filter_by(customer_id=customer.id).count(), 1)

    def test_failure_rolls_back_balance_transaction_and_ledger(self):
        payload = {"token": self.points_token, "action": "earn_points",
                   "purchase_amount": 25, "request_id": "rollback"}
        with patch("app.services.loyalty_transaction_service.LoyaltyLedgerEntry", side_effect=RuntimeError("simulated")):
            with self.assertRaises(RuntimeError):
                self._request(payload, self.points_headers)
        db.session.remove()
        customer = db.session.get(Customer, self.points_customer_id)
        self.assertEqual(customer.current_points, 100)
        self.assertEqual(Transaction.query.count(), 0)
        self.assertEqual(LoyaltyLedgerEntry.query.count(), 0)

    def test_reconciliation_detects_tampering_without_repairing(self):
        payload = {"token": self.points_token, "action": "earn_points",
                   "purchase_amount": 10, "request_id": "reconcile"}
        self.assertEqual(self._request(payload, self.points_headers)[0], 200)
        customer = db.session.get(Customer, self.points_customer_id)
        self.assertTrue(reconcile_customer_balance(customer)["consistent"])
        customer.current_points = 999; db.session.commit()
        result = reconcile_customer_balance(customer)
        self.assertFalse(result["consistent"])
        self.assertEqual(customer.current_points, 999)


if __name__ == "__main__":
    unittest.main()
