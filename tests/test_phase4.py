import io
import json
import zipfile
import unittest
from app import create_app
from app.extensions import db
from app.models import Business, Customer, AppleDevice
from app.services import AppleWalletService, GoogleWalletService


class Phase4TestCase(unittest.TestCase):
    """Test suite for Phase 4: Apple Web Service Protocol, Google Wallet REST PATCH, and Geofencing."""

    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.app_context = self.app.app_context()
        self.app_context.push()
        db.create_all()

        # Seed sample business with geolocation (CDMX coordinates)
        self.business = Business(
            name="Taquería Los Volcanes",
            slug="taqueria-volcanes",
            primary_color="#DC2626",
            secondary_color="#FEF08A",
            loyalty_type="stamps",
            stamps_reward_limit=6,
            latitude=19.4194,
            longitude=-99.1656,
        )
        db.session.add(self.business)
        db.session.commit()

        # Seed sample customer
        self.customer = Customer(
            business_id=self.business.id,
            full_name="Diego Rivera",
            email="diego@arte.mx",
            phone="+525511223344",
            current_stamps=3,
        )
        db.session.add(self.customer)
        db.session.commit()

        self.apple_service = AppleWalletService()
        self.serial_number = self.apple_service.get_serial_number(self.customer)
        self.pass_type_id = self.apple_service.pass_type_id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.app_context.pop()

    def test_geofencing_payloads(self):
        """Test Apple and Google Wallet services include geofencing coordinates when configured."""
        # 1. Apple Wallet Geofencing
        pass_json = self.apple_service.build_pass_json(self.customer)
        self.assertIn("locations", pass_json)
        self.assertEqual(len(pass_json["locations"]), 1)
        self.assertEqual(pass_json["locations"][0]["latitude"], 19.4194)
        self.assertEqual(pass_json["locations"][0]["longitude"], -99.1656)
        self.assertIn("Taquería Los Volcanes", pass_json["locations"][0]["relevantText"])

        # 2. Google Wallet Geofencing
        google_service = GoogleWalletService()
        loyalty_obj = google_service.create_loyalty_object(self.customer)
        self.assertIn("locations", loyalty_obj)
        self.assertEqual(loyalty_obj["locations"][0]["latitude"], 19.4194)

    def test_apple_device_registration_lifecycle(self):
        """Test Apple Wallet Web Service device registration, pass fetch, update listing, and unregistration."""
        device_id = "device_test_lib_12345"
        push_token = "apns_dummy_token_abcde12345"

        # 1. Register device with valid auth token
        reg_url = f"/api/apple/v1/devices/{device_id}/registrations/{self.pass_type_id}/{self.serial_number}"
        res = self.client.post(
            reg_url,
            json={"pushToken": push_token},
            headers={"Authorization": f"ApplePass {self.customer.qr_code_token}"},
        )
        self.assertEqual(res.status_code, 201)

        # Verify in DB
        device = AppleDevice.query.filter_by(device_library_identifier=device_id).first()
        self.assertIsNotNone(device)
        self.assertEqual(device.push_token, push_token)
        self.assertEqual(device.customer_id, self.customer.id)

        # 2. Re-register (idempotent 200 OK)
        res_re = self.client.post(
            reg_url,
            json={"pushToken": push_token},
            headers={"Authorization": f"ApplePass {self.customer.qr_code_token}"},
        )
        self.assertEqual(res_re.status_code, 200)

        # 3. Unauthorized registration with wrong token
        res_fail = self.client.post(
            reg_url,
            json={"pushToken": push_token},
            headers={"Authorization": "ApplePass invalid_token_123"},
        )
        self.assertEqual(res_fail.status_code, 401)

        # 4. Get updated passes list
        list_url = f"/api/apple/v1/devices/{device_id}/passes/{self.pass_type_id}"
        res_list = self.client.get(list_url)
        self.assertEqual(res_list.status_code, 200)
        data_list = res_list.get_json()
        self.assertIn(self.serial_number, data_list["serialNumbers"])

        # 5. Deliver pass to Apple device
        deliver_url = f"/api/apple/v1/passes/{self.pass_type_id}/{self.serial_number}"
        res_deliv = self.client.get(
            deliver_url,
            headers={"Authorization": f"ApplePass {self.customer.qr_code_token}"},
        )
        self.assertEqual(res_deliv.status_code, 200)
        self.assertEqual(res_deliv.mimetype, "application/vnd.apple.pkpass")
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(res_deliv.data)))

        # 6. Unregister device (DELETE)
        res_del = self.client.delete(
            reg_url,
            headers={"Authorization": f"ApplePass {self.customer.qr_code_token}"},
        )
        self.assertEqual(res_del.status_code, 200)
        self.assertIsNone(AppleDevice.query.filter_by(device_library_identifier=device_id).first())

    def test_google_wallet_patch_structure(self):
        """Test GoogleWalletService update_loyalty_object returns valid patch payload."""
        google_service = GoogleWalletService()
        result = google_service.update_loyalty_object(self.customer)

        self.assertIn(result.get("status"), ["mock_success", "success"])
        self.assertIn("object_id", result)
        if result.get("status") == "mock_success":
            self.assertEqual(
                result["patch_body"]["loyaltyPoints"]["balance"]["string"],
                f"{self.customer.current_stamps} / {self.business.stamps_reward_limit}",
            )

    def test_scanner_real_time_sync_triggers(self):
        """Test that scanner transaction execution triggers sync routines for registered devices."""
        # Register a mock Apple Device for the customer
        device = AppleDevice(
            customer_id=self.customer.id,
            device_library_identifier="dev_trigger_123",
            pass_type_identifier=self.pass_type_id,
            serial_number=self.serial_number,
            push_token="sample_push_token",
        )
        db.session.add(device)
        db.session.commit()

        # Perform scanner transaction (+1 stamp)
        res = self.client.post(
            "/api/scanner/process",
            json={
                "customer_id": self.customer.id,
                "business_id": self.business.id,
                "action": "earn_stamp",
                "amount": 1,
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["customer"]["current_stamps"], 4)
        self.assertIn("wallet_sync", data)
        self.assertEqual(data["wallet_sync"]["apple_devices_notified"], 1)
        self.assertEqual(data["wallet_sync"]["google_status"], "mock_success")


if __name__ == "__main__":
    unittest.main()
