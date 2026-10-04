import unittest
import jwt
from app import create_app
from app.extensions import db
from app.models import Business, Customer
from app.services import GoogleWalletService


class GoogleWalletTestCase(unittest.TestCase):
    """Test suite for Google Wallet service and endpoints."""

    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Seed sample business
        self.business_stamps = Business(
            name="Café Central",
            slug="cafe-central",
            logo_url="https://example.com/logo.png",
            primary_color="#4A2E18",
            secondary_color="#D4A373",
            loyalty_type="stamps",
            stamps_reward_limit=8,
        )
        self.business_points = Business(
            name="Boutique Elegance",
            slug="boutique-elegance",
            primary_color="#1E3A8A",
            secondary_color="#FFFFFF",
            loyalty_type="points",
            points_per_currency=2.0,
        )
        db.session.add_all([self.business_stamps, self.business_points])
        db.session.commit()

        # Seed sample customers
        self.customer_stamps = Customer(
            business_id=self.business_stamps.id,
            full_name="Carlos Mendoza",
            email="carlos@example.com",
            phone="+5215512345678",
            current_stamps=5,
        )
        self.customer_points = Customer(
            business_id=self.business_points.id,
            full_name="Ana Lopez",
            email="ana@example.com",
            current_points=350.0,
        )
        db.session.add_all([self.customer_stamps, self.customer_points])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_google_wallet_service_jwt_generation(self):
        """Test GoogleWalletService generates a valid RS256 JWT and Save URL."""
        service = GoogleWalletService()
        token, save_url = service.create_jwt_pass(self.customer_stamps)

        self.assertTrue(save_url.startswith("https://pay.google.com/gp/v/save/"))
        self.assertIn(token, save_url)

        # Decode unverified to inspect payload structure
        decoded = jwt.decode(token, options={"verify_signature": False})
        self.assertEqual(decoded.get("aud"), "google")
        self.assertEqual(decoded.get("typ"), "savetowallet")
        self.assertIn("payload", decoded)

        payload_data = decoded["payload"]
        self.assertIn("loyaltyClasses", payload_data)
        self.assertIn("loyaltyObjects", payload_data)

        # Check LoyaltyClass
        loyalty_class = payload_data["loyaltyClasses"][0]
        self.assertIn(self.business_stamps.slug.replace("-", "_"), loyalty_class["id"])
        self.assertEqual(loyalty_class["issuerName"], "Café Central")
        self.assertEqual(loyalty_class["hexBackgroundColor"], "#4A2E18")

        # Check LoyaltyObject
        loyalty_object = payload_data["loyaltyObjects"][0]
        self.assertEqual(loyalty_object["accountName"], "Carlos Mendoza")
        self.assertEqual(loyalty_object["barcode"]["value"], self.customer_stamps.qr_code_token)
        self.assertEqual(loyalty_object["loyaltyPoints"]["balance"]["string"], "5 / 8")

    def test_google_wallet_points_program(self):
        """Test GoogleWalletService for a points-based loyalty business."""
        service = GoogleWalletService()
        token, save_url = service.create_jwt_pass(self.customer_points)

        decoded = jwt.decode(token, options={"verify_signature": False})
        loyalty_object = decoded["payload"]["loyaltyObjects"][0]
        self.assertEqual(loyalty_object["loyaltyPoints"]["label"], "Puntos")
        self.assertEqual(loyalty_object["loyaltyPoints"]["balance"]["string"], "350 pts")

    def test_google_wallet_endpoint_success(self):
        """Test GET /api/customers/<id>/wallet/google endpoint with valid customer."""
        response = self.client.get(f"/api/customers/{self.customer_stamps.id}/wallet/google")
        self.assertEqual(response.status_code, 200)

        data = response.get_json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["customer_id"], self.customer_stamps.id)
        self.assertEqual(data["business_name"], "Café Central")
        self.assertTrue(data["save_url"].startswith("https://pay.google.com/gp/v/save/"))
        self.assertIn("jwt", data)

    def test_google_wallet_endpoint_not_found(self):
        """Test GET /api/customers/<id>/wallet/google with non-existing customer."""
        response = self.client.get("/api/customers/99999/wallet/google")
        self.assertEqual(response.status_code, 404)
        data = response.get_json()
        self.assertEqual(data["status"], "error")


if __name__ == "__main__":
    unittest.main()
