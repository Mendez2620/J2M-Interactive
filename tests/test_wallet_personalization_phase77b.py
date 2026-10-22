import io
import json
import unittest
import zipfile

from app import create_app
from app.extensions import db
from app.models import Business, Customer, PlatformAdmin
from app.services.apple_wallet import AppleWalletService
from app.services.google_wallet import GoogleWalletService


class WalletPersonalizationPhase77BTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        self.business = Business(name="Café Uno", slug="cafe-uno", loyalty_type="stamps", stamps_reward_limit=8)
        self.other = Business(name="Café Dos", slug="cafe-dos", loyalty_type="points")
        db.session.add_all([self.business, self.other]); db.session.flush()
        self.customer = Customer(
            business_id=self.business.id, full_name="Persona Real",
            current_stamps=3, current_points=12, qr_code_token="immutable-wallet-qr",
        )
        admin = PlatformAdmin(name="Admin", email="wallet-admin@example.test", is_active=True)
        admin.set_password("AdminSeguro123")
        db.session.add_all([self.customer, admin]); db.session.commit()
        self.admin_id = admin.id
        with self.client.session_transaction() as browser:
            browser["platform_admin_id"] = admin.id

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.context.pop()

    def payload(self):
        return {
            "wallet_program_name": "Club Café Uno",
            "wallet_card_text": "Cada visita cuenta",
            "wallet_text_color": "#F8FAFC",
            "wallet_balance_label": "Mis sellos",
            "wallet_instructions": "Presenta tu tarjeta antes de pagar.",
            "wallet_terms": "Válido en sucursales participantes.",
            "wallet_contact": "soporte@cafe.example",
            "wallet_preset": "bold",
        }

    def test_super_admin_saves_valid_design_without_touching_identity_or_balance(self):
        before = (self.customer.id, self.customer.qr_code_token, self.customer.current_stamps, self.customer.current_points)
        response = self.client.post(f"/admin/business/{self.business.id}/update", json=self.payload())
        self.assertEqual(response.status_code, 200)
        db.session.refresh(self.business); db.session.refresh(self.customer); db.session.refresh(self.other)
        self.assertEqual(self.business.wallet_program_name, "Club Café Uno")
        self.assertEqual(self.business.wallet_text_color, "#F8FAFC")
        self.assertEqual((self.customer.id, self.customer.qr_code_token, self.customer.current_stamps, self.customer.current_points), before)
        self.assertIsNone(self.other.wallet_program_name)

    def test_invalid_design_authentication_and_csrf_are_rejected(self):
        invalid = self.payload(); invalid.update(wallet_text_color="red", wallet_card_text="<script>alert(1)</script>")
        self.assertEqual(self.client.post(f"/admin/business/{self.business.id}/update", json=invalid).status_code, 400)
        invalid_url = self.payload(); invalid_url["logo_url"] = "http://insecure.example/logo.png"
        self.assertEqual(self.client.post(f"/admin/business/{self.business.id}/update", json=invalid_url).status_code, 400)
        self.assertIsNone(self.business.wallet_program_name)
        anonymous = self.app.test_client()
        self.assertEqual(anonymous.post(f"/admin/business/{self.business.id}/update", json=self.payload()).status_code, 401)
        self.app.config["CSRF_ENABLED"] = True
        self.assertEqual(self.client.post(f"/admin/business/{self.business.id}/update", json=self.payload()).status_code, 400)
        with self.client.session_transaction() as browser:
            browser["csrf_token"] = "wallet-csrf"
        accepted = self.client.post(
            f"/admin/business/{self.business.id}/update", json=self.payload(),
            headers={"X-CSRFToken": "wallet-csrf"},
        )
        self.assertEqual(accepted.status_code, 200)

    def test_apple_and_google_use_customization_with_stable_ids_qr_and_balances(self):
        for key, value in self.payload().items():
            setattr(self.business, key, value)
        db.session.commit()
        apple = AppleWalletService()
        serial = apple.get_serial_number(self.customer)
        pass_json = apple.build_pass_json(self.customer)
        self.assertEqual(pass_json["serialNumber"], serial)
        self.assertEqual(pass_json["barcode"]["message"], "immutable-wallet-qr")
        self.assertEqual(pass_json["foregroundColor"], "rgb(248, 250, 252)")
        self.assertEqual(pass_json["storeCard"]["primaryFields"][0]["label"], "MIS SELLOS")
        self.assertEqual(pass_json["storeCard"]["primaryFields"][0]["value"], "3 / 8")
        with zipfile.ZipFile(apple.generate_pkpass(self.customer)) as archive:
            embedded = json.loads(archive.read("pass.json"))
            self.assertEqual(embedded["serialNumber"], serial)

        google = GoogleWalletService()
        object_id = google.get_object_id(self.customer)
        loyalty_class = google.create_or_update_class(self.business)
        loyalty_object = google.create_loyalty_object(self.customer)
        self.assertEqual(google.get_object_id(self.customer), object_id)
        self.assertEqual(loyalty_class["programName"], "Club Café Uno")
        self.assertEqual(loyalty_object["barcode"]["value"], "immutable-wallet-qr")
        self.assertEqual(loyalty_object["loyaltyPoints"]["balance"]["string"], "3 / 8")
        self.assertIn("Cada visita cuenta", json.dumps(loyalty_object, ensure_ascii=False))

    def test_historical_business_defaults_and_editor_preview_are_safe(self):
        page = self.client.get(f"/admin/business/{self.business.id}")
        text = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Diseño profesional Wallet", text)
        self.assertIn("Cliente de ejemplo", text)
        self.assertIn("QR FICTICIO", text)
        self.assertIn("data-wallet-preview=\"apple\"", text)
        self.assertIn("data-wallet-preview=\"google\"", text)
        self.assertIn("grid-cols-1", text)
        self.assertNotIn("immutable-wallet-qr", text)
        legacy = AppleWalletService().build_pass_json(self.customer)
        self.assertEqual(legacy["storeCard"]["primaryFields"][0]["label"], "SELLOS")


if __name__ == "__main__":
    unittest.main()
