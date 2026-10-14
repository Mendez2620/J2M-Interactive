import os
import unittest
from unittest.mock import patch

from app import create_app


class ProductionConfigTestCase(unittest.TestCase):
    def test_production_rejects_default_or_short_secret(self):
        for secret in ("default-dev-secret-key-j2m", "short"):
            with self.subTest(secret=secret), patch.dict(os.environ, {"SECRET_KEY": secret}, clear=False):
                with self.assertRaises(RuntimeError):
                    create_app("production")

    def test_production_forces_debug_off_with_robust_secret(self):
        with patch.dict(os.environ, {"SECRET_KEY": "a-robust-production-secret-key-with-entropy-123", "FLASK_DEBUG": "true"}, clear=False):
            app = create_app("production")
            self.assertFalse(app.debug)


if __name__ == "__main__":
    unittest.main()
