import io
import json
import zipfile
import hashlib
import unittest
from app import create_app
from app.extensions import db
from app.models import Business, Customer
from app.services import AppleWalletService
from app.services.apple_wallet import hex_to_rgb


class AppleWalletTestCase(unittest.TestCase):
    """Test suite for Apple Wallet .pkpass service and endpoints."""

    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Seed sample business (stamps)
        self.business_stamps = Business(
            name="Café Central",
            slug="cafe-central",
            primary_color="#4A2E18",
            secondary_color="#D4A373",
            loyalty_type="stamps",
            stamps_reward_limit=10,
        )
        # Seed sample business (points)
        self.business_points = Business(
            name="Restaurante Gourmet",
            slug="restaurante-gourmet",
            primary_color="#1E3A8A",
            secondary_color="#FFFFFF",
            loyalty_type="points",
            points_per_currency=1.5,
        )
        db.session.add_all([self.business_stamps, self.business_points])
        db.session.commit()

        # Seed sample customers
        self.customer_stamps = Customer(
            business_id=self.business_stamps.id,
            full_name="Carlos Mendoza",
            email="carlos@example.com",
            phone="+5215512345678",
            current_stamps=7,
        )
        self.customer_points = Customer(
            business_id=self.business_points.id,
            full_name="Maria Garcia",
            email="maria@example.com",
            current_points=450.0,
        )
        db.session.add_all([self.customer_stamps, self.customer_points])
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_hex_to_rgb_converter(self):
        """Test conversion of hex colors to Apple-compliant RGB strings."""
        self.assertEqual(hex_to_rgb("#4A2E18"), "rgb(74, 46, 24)")
        self.assertEqual(hex_to_rgb("#FFFFFF"), "rgb(255, 255, 255)")
        self.assertEqual(hex_to_rgb("#000"), "rgb(0, 0, 0)")
        self.assertEqual(hex_to_rgb("invalid"), "rgb(30, 58, 138)")

    def test_build_pass_json_structure(self):
        """Test pass.json dictionary structure for stamps and points."""
        service = AppleWalletService()
        pass_json = service.build_pass_json(self.customer_stamps)

        self.assertEqual(pass_json["formatVersion"], 1)
        self.assertEqual(pass_json["organizationName"], "Café Central")
        self.assertIn("storeCard", pass_json)

        store_card = pass_json["storeCard"]
        primary = store_card["primaryFields"][0]
        self.assertEqual(primary["key"], "stamps_balance")
        self.assertEqual(primary["value"], "7 / 10")

        # Barcode
        self.assertEqual(pass_json["barcode"]["format"], "PKBarcodeFormatQR")
        self.assertEqual(pass_json["barcode"]["message"], self.customer_stamps.qr_code_token)

    def test_generate_pkpass_zip_integrity(self):
        """Test generate_pkpass outputs a valid ZIP containing required Apple Wallet files."""
        service = AppleWalletService()
        pkpass_stream = service.generate_pkpass(self.customer_stamps)

        self.assertIsInstance(pkpass_stream, io.BytesIO)
        self.assertGreater(len(pkpass_stream.getvalue()), 0)

        # Verify ZIP bundle contents
        with zipfile.ZipFile(pkpass_stream, "r") as zf:
            file_list = zf.namelist()
            expected_files = [
                "pass.json",
                "manifest.json",
                "signature",
                "icon.png",
                "icon@2x.png",
                "logo.png",
                "logo@2x.png",
                "strip.png",
                "strip@2x.png",
            ]
            for ef in expected_files:
                self.assertIn(ef, file_list)

            # Check manifest SHA-1 checksum integrity
            manifest_content = json.loads(zf.read("manifest.json").decode("utf-8"))
            for asset_name, expected_hash in manifest_content.items():
                self.assertIn(asset_name, file_list)
                asset_bytes = zf.read(asset_name)
                actual_hash = hashlib.sha1(asset_bytes).hexdigest()
                self.assertEqual(actual_hash, expected_hash)

    def test_apple_wallet_endpoint_download(self):
        """Test GET /api/customers/<id>/wallet/apple returns a downloadable .pkpass file."""
        response = self.client.get(f"/api/customers/{self.customer_stamps.id}/wallet/apple")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/vnd.apple.pkpass")
        self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
        self.assertIn("cafe-central-pass.pkpass", response.headers.get("Content-Disposition", ""))

        # Verify body is a valid ZIP
        pkpass_bytes = io.BytesIO(response.data)
        self.assertTrue(zipfile.is_zipfile(pkpass_bytes))

    def test_apple_wallet_endpoint_not_found(self):
        """Test GET /api/customers/<id>/wallet/apple returns 404 for non-existent customer."""
        response = self.client.get("/api/customers/99999/wallet/apple")
        self.assertEqual(response.status_code, 404)
        data = response.get_json()
        self.assertEqual(data["status"], "error")


if __name__ == "__main__":
    unittest.main()
