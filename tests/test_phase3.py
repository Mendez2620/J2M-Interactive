import json
import unittest
from datetime import date
from app import create_app
from app.extensions import db
from app.models import Business, Customer, Staff, Transaction


class Phase3TestCase(unittest.TestCase):
    """Test suite for Phase 3: Public Join with Birthdate, Super Admin, and Adaptive Scanner."""

    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Seed sample businesses
        self.cafe = Business(
            name="Café Aroma",
            slug="cafe-aroma",
            primary_color="#78350F",
            secondary_color="#FEF3C7",
            loyalty_type="stamps",
            stamps_reward_limit=5,
            latitude=19.4326,
            longitude=-99.1332,
        )
        self.boutique = Business(
            name="Moda Urbana",
            slug="moda-urbana",
            primary_color="#4F46E5",
            secondary_color="#EEF2FF",
            loyalty_type="points",
            points_per_currency=2.0,
            latitude=19.4200,
            longitude=-99.1600,
        )
        db.session.add_all([self.cafe, self.boutique])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_public_join_with_birthdate(self):
        """Test customer registration at /join/<slug> with birthdate and wallet links generation."""
        # 1. GET page
        response = self.client.get("/join/cafe-aroma")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Caf\xc3\xa9 Aroma", response.data)

        # 2. POST registration
        payload = {
            "full_name": "Valeria Sanchez",
            "email": "valeria@test.com",
            "phone": "+525598765432",
            "birthdate": "1998-05-15",
        }
        res = self.client.post(
            "/join/cafe-aroma",
            data=json.dumps(payload),
            content_type="application/json",
            headers={"Accept": "application/json"},
        )
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertIn("google_wallet_url", data)
        self.assertIn("apple_wallet_url", data)
        self.assertEqual(data["customer"]["birthdate"], "1998-05-15")

        # Verify DB customer
        customer = Customer.query.filter_by(email="valeria@test.com").first()
        self.assertIsNotNone(customer)
        self.assertEqual(customer.birthdate, date(1998, 5, 15))
        self.assertEqual(customer.business_id, self.cafe.id)

    def test_admin_dashboard_metrics_and_age_distribution(self):
        """Test super admin metrics including birthdate-based age grouping."""
        # Add customers of various ages
        c1 = Customer(
            business_id=self.cafe.id,
            full_name="Adult 1 (22yo)",
            email="c1@test.com",
            birthdate=date(2004, 1, 1),  # ~22 years old
        )
        c2 = Customer(
            business_id=self.boutique.id,
            full_name="Adult 2 (30yo)",
            email="c2@test.com",
            birthdate=date(1996, 1, 1),  # ~30 years old
        )
        c3 = Customer(
            business_id=self.boutique.id,
            full_name="Adult 3 (No birthdate)",
            email="c3@test.com",
            birthdate=None,
        )
        db.session.add_all([c1, c2, c3])
        db.session.commit()

        res = self.client.get("/admin/dashboard", headers={"Accept": "application/json"})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["metrics"]["total_businesses"], 2)
        self.assertEqual(data["metrics"]["total_customers"], 3)
        self.assertEqual(data["metrics"]["age_distribution"]["18-24"], 1)
        self.assertEqual(data["metrics"]["age_distribution"]["25-34"], 1)
        self.assertEqual(data["metrics"]["age_distribution"]["desconocido"], 1)

    def test_staff_creation_and_password_auth(self):
        """Test staff creation endpoint with Manager and Cashier roles and password hashing."""
        # Create Manager
        res = self.client.post(
            f"/admin/business/{self.cafe.id}/staff",
            json={
                "name": "Gerente Carlos",
                "username": "carlos_gerente",
                "role": "manager",
                "password": "SecurePassword123!",
                "email": "carlos@cafe.com",
            },
        )
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertEqual(data["staff"]["role"], "manager")

        # Verify DB and password check
        staff = Staff.query.filter_by(username="carlos_gerente").first()
        self.assertIsNotNone(staff)
        self.assertTrue(staff.check_password("SecurePassword123!"))
        self.assertFalse(staff.check_password("WrongPassword"))

        # Create Cashier for Boutique
        res_cashier = self.client.post(
            f"/admin/business/{self.boutique.id}/staff",
            json={
                "name": "Cajero Juan",
                "username": "juan_cajero",
                "role": "cashier",
                "password": "CashierPass456!",
            },
        )
        self.assertEqual(res_cashier.status_code, 201)
        cashier = Staff.query.filter_by(username="juan_cajero").first()
        self.assertEqual(cashier.role, "cashier")

    def test_scanner_stamps_earn_and_redeem(self):
        """Test scanner QR lookup and stamp transactions (+1 stamp and reward redemption)."""
        customer = Customer(
            business_id=self.cafe.id,
            full_name="Pedro Pascal",
            email="pedro@cafe.com",
            current_stamps=4,
        )
        db.session.add(customer)
        db.session.commit()

        # 1. Lookup by QR token
        lookup_res = self.client.post(
            "/api/scanner/lookup",
            json={"token": customer.qr_code_token, "business_id": self.cafe.id},
        )
        self.assertEqual(lookup_res.status_code, 200)
        self.assertEqual(lookup_res.get_json()["customer"]["full_name"], "Pedro Pascal")

        # 2. Earn +1 Stamp -> 4 + 1 = 5 (reaches reward limit 5)
        earn_res = self.client.post(
            "/api/scanner/process",
            json={
                "customer_id": customer.id,
                "business_id": self.cafe.id,
                "action": "earn_stamp",
                "amount": 1,
            },
        )
        self.assertEqual(earn_res.status_code, 200)
        self.assertEqual(earn_res.get_json()["customer"]["current_stamps"], 5)

        # 3. Redeem Reward (costs 5 stamps) -> 5 - 5 = 0
        redeem_res = self.client.post(
            "/api/scanner/process",
            json={
                "customer_id": customer.id,
                "business_id": self.cafe.id,
                "action": "redeem_stamp",
                "amount": 5,
            },
        )
        self.assertEqual(redeem_res.status_code, 200)
        self.assertEqual(redeem_res.get_json()["customer"]["current_stamps"], 0)

        # 4. Attempt to redeem when stamps are 0 -> should fail 400
        fail_res = self.client.post(
            "/api/scanner/process",
            json={
                "customer_id": customer.id,
                "business_id": self.cafe.id,
                "action": "redeem_stamp",
                "amount": 5,
            },
        )
        self.assertEqual(fail_res.status_code, 400)

    def test_scanner_points_earn_and_redeem(self):
        """Test scanner points accrual (based on purchase ratio) and points redemption."""
        customer = Customer(
            business_id=self.boutique.id,
            full_name="Lucia Méndez",
            email="lucia@boutique.com",
            current_points=50.0,
        )
        db.session.add(customer)
        db.session.commit()

        # 1. Earn Points: Purchase $100 -> Ratio 2.0 -> 200 points + 50 initial = 250 points
        earn_res = self.client.post(
            "/api/scanner/process",
            json={
                "customer_id": customer.id,
                "business_id": self.boutique.id,
                "action": "earn_points",
                "amount": 0,
                "purchase_amount": 100.0,
            },
        )
        self.assertEqual(earn_res.status_code, 200)
        self.assertEqual(earn_res.get_json()["customer"]["current_points"], 250.0)

        # 2. Redeem 100 Points -> 250 - 100 = 150 points
        redeem_res = self.client.post(
            "/api/scanner/process",
            json={
                "customer_id": customer.id,
                "business_id": self.boutique.id,
                "action": "redeem_points",
                "amount": 100.0,
            },
        )
        self.assertEqual(redeem_res.status_code, 200)
        self.assertEqual(redeem_res.get_json()["customer"]["current_points"], 150.0)

        # 3. Attempt to redeem more than balance (e.g. 500 pts) -> 400
        fail_res = self.client.post(
            "/api/scanner/process",
            json={
                "customer_id": customer.id,
                "business_id": self.boutique.id,
                "action": "redeem_points",
                "amount": 500.0,
            },
        )
        self.assertEqual(fail_res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
