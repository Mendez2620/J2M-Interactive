import os
import unittest
from unittest.mock import patch

from app import create_app
from app.config import ProductionConfig


class ProductionConfigTestCase(unittest.TestCase):
    def test_production_rejects_default_or_short_secret(self):
        for secret in ("default-dev-secret-key-j2m", "short"):
            with self.subTest(secret=secret), patch.dict(os.environ, {"SECRET_KEY": secret}, clear=False):
                with self.assertRaises(RuntimeError):
                    create_app("production")

    def test_production_rejects_reused_session_and_jwt_secret(self):
        shared = "shared-production-secret-with-at-least-32-characters"
        with patch.dict(os.environ, {"SECRET_KEY": shared, "JWT_SECRET_KEY": shared,
                                    "CORS_ORIGINS": "https://app.example.com"}, clear=False):
            with self.assertRaises(RuntimeError):
                create_app("production")

    def test_production_forces_debug_off_with_robust_secret(self):
        with patch.dict(os.environ, {"SECRET_KEY": "a-robust-production-secret-key-with-entropy-123",
                                    "JWT_SECRET_KEY": "another-independent-jwt-key-with-entropy-456",
                                    "CORS_ORIGINS": "https://app.example.com",
                                    "PUBLIC_BASE_URL": "https://app.example.com",
                                    "FLASK_DEBUG": "true"}, clear=False), \
             patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"):
            app = create_app("production")
            self.assertFalse(app.debug)
            client = app.test_client()
            self.assertEqual(client.get("/api/health").status_code, 200)
            self.assertEqual(client.get("/admin/login").status_code, 200)
            self.assertEqual(client.get("/scanner").status_code, 404)
            self.assertIn("max-age=31536000", client.get("/api/health").headers["Strict-Transport-Security"])


if __name__ == "__main__":
    unittest.main()
