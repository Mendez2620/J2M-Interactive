import unittest
from unittest.mock import patch

from app.services.google_wallet import GoogleWalletService
from scripts import check_wallet_demo_readiness as demo


class WalletDemoReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.credentials = {
            "type": "service_account", "project_id": "synthetic",
            "client_email": "wallet@synthetic.iam.gserviceaccount.com",
            "private_key": GoogleWalletService._generate_dev_private_key(),
            "token_uri": "https://oauth2.googleapis.com/token",
        }

    def test_rejects_operational_issuer_insecure_origin_and_bad_credentials(self):
        cases = [
            ("123", "123", "https://demo.example", self.credentials),
            ("123", "456", "http://demo.example", self.credentials),
            ("123", "456", "https://demo.example", {**self.credentials, "private_key": "invalid"}),
            ("123", "456", "https://demo.example", {**self.credentials, "token_uri": "https://invalid.example"}),
        ]
        for args in cases:
            with self.subTest(issuer=args[0], origin=args[2]):
                self.assertFalse(demo.valid_candidate(*args))
        self.assertTrue(demo.valid_candidate("123", "456", "https://demo.example", self.credentials))

    @patch.object(demo, "valid_candidate", return_value=True)
    @patch.object(demo, "runtime")
    @patch.object(demo.Path, "read_text", return_value="{}")
    def test_structural_success_cannot_authorize_real_demo(self, read, runtime, candidate):
        config = dict(J2M_DEMO_CREDENTIALS_FILE="synthetic.json", J2M_DEMO_ISSUER="123",
                      J2M_OPERATIONAL_ISSUER="456", J2M_DEMO_HTTPS_ORIGIN="https://demo.example")
        with patch.dict(demo.os.environ, config), patch("builtins.print"), patch("sys.argv", ["preflight"]):
            self.assertEqual(demo.main(), 2)
