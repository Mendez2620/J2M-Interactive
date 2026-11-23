"""Release identity probes remain backward-compatible but fail closed on invalid pins."""

import os
import unittest
from unittest.mock import patch

from flask import Flask
from sqlalchemy.exc import SQLAlchemyError

from app.routes import main_bp


SHA = "a" * 40


class ReleaseIdentityTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(main_bp)
        self.client = self.app.test_client()

    def test_pinned_release_is_present_in_both_health_probes(self):
        with patch.dict(os.environ, {"J2M_RELEASE_IMAGE": "j2m-interactive:" + SHA}):
            health = self.client.get("/api/health")
            with patch("app.routes.db.session.execute", return_value=None):
                ready = self.client.get("/api/ready")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(health.headers.get("X-J2M-Revision"), SHA)
        self.assertEqual(ready.headers.get("X-J2M-Revision"), SHA)
        self.assertEqual(health.json["status"], "ok")
        self.assertEqual(ready.json["status"], "ready")
        self.assertEqual(health.headers["Cache-Control"], "no-store")

    def test_unpinned_and_malformed_images_never_claim_a_release(self):
        for value in ("", "j2m-interactive:main", "j2m-interactive:" + "x" * 40,
                      "other-image:" + SHA):
            with self.subTest(value=value), patch.dict(
                os.environ, {"J2M_RELEASE_IMAGE": value}
            ):
                health = self.client.get("/api/health")
                self.assertEqual(health.status_code, 200)
                self.assertIsNone(health.headers.get("X-J2M-Revision"))

    def test_database_failure_preserves_unavailable_status_and_revision(self):
        with patch.dict(os.environ, {"J2M_RELEASE_IMAGE": "j2m-interactive:" + SHA}), \
             patch("app.routes.db.session.execute", side_effect=SQLAlchemyError("down")), \
             patch("app.routes.db.session.rollback", return_value=None):
            result = self.client.get("/api/ready")
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.headers.get("X-J2M-Revision"), SHA)
        self.assertEqual(result.json["database"], "unavailable")


if __name__ == "__main__":
    unittest.main()
