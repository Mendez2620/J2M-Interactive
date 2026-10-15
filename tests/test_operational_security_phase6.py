import os
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from app import create_app
from app.config import ProductionConfig, TestingConfig
from app.extensions import db
from app.models import AuthRateLimitSlot, Business, PlatformAdmin, Staff


class OperationalSecurityPhase6TestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.uri = f"sqlite:///{(Path(self.directory.name) / 'security.sqlite').as_posix()}"
        with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", self.uri):
            self.app = create_app("testing")
        self.app.add_url_rule("/_test/error", "phase6_test_error", self._raise_test_error, methods=["GET", "POST"])
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        business = Business(name="Seguro", slug="seguro", is_active=True,
                            web_access_enabled=True, mobile_access_enabled=True)
        admin = PlatformAdmin(name="Admin", email="admin@example.test", is_active=True)
        admin.set_password("AdminPassword123!")
        db.session.add_all([business, admin]); db.session.flush()
        manager = self._staff(business, "manager", "manager", True, True)
        cashier = self._staff(business, "cashier", "cashier", False, True)
        db.session.commit()
        self.app.config.update(AUTH_RATE_LIMIT_ATTEMPTS=2, AUTH_RATE_LIMIT_WINDOW=300)

    def tearDown(self):
        db.session.remove(); db.drop_all(); db.engine.dispose(); self.ctx.pop(); self.directory.cleanup()

    def _staff(self, business, username, role, web, mobile):
        staff = Staff(business_id=business.id, username=username, role=role, is_active=True,
                      web_access_enabled=web, mobile_access_enabled=mobile,
                      permissions="scanner_operate" if role == "cashier" else "view_stats")
        staff.set_password("StaffPassword123!"); db.session.add(staff)
        return staff

    @staticmethod
    def _raise_test_error():
        raise RuntimeError("internal-only")

    def test_all_login_surfaces_use_persistent_identity_and_ip_limits(self):
        client = self.app.test_client()
        for _ in range(2):
            self.assertEqual(client.post("/admin/login", data={"email": "admin@example.test", "password": "wrong"}).status_code, 401)
        self.assertEqual(client.post("/admin/login", data={"email": "admin@example.test", "password": "wrong"}).status_code, 429)

        for _ in range(2):
            self.assertEqual(client.post("/manager/login", data={"identity": "manager", "business_slug": "seguro", "password": "wrong"}).status_code, 401)
        self.assertEqual(client.post("/manager/login", data={"identity": "manager", "business_slug": "seguro", "password": "wrong"}).status_code, 429)

        for _ in range(2):
            self.assertEqual(client.post("/api/scanner/login", json={"username": "cashier", "business_slug": "seguro", "password": "wrong"}).status_code, 401)
        self.assertEqual(client.post("/api/scanner/login", json={"username": "cashier", "business_slug": "seguro", "password": "wrong"}).status_code, 429)
        self.assertTrue(all(len(row.key_hash) == 64 for row in AuthRateLimitSlot.query.all()))

    def test_concurrent_limit_is_shared_between_app_instances(self):
        with patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", self.uri):
            second_app = create_app("testing")
        second_app.config.update(AUTH_RATE_LIMIT_ATTEMPTS=2, AUTH_RATE_LIMIT_WINDOW=300)
        barrier = Barrier(3)

        def attempt(index):
            app = self.app if index % 2 == 0 else second_app
            with app.test_client() as client:
                barrier.wait(timeout=10)
                return client.post("/api/scanner/login", json={
                    "username": "cashier", "business_slug": "seguro", "password": "wrong",
                }, environ_base={"REMOTE_ADDR": "192.0.2.10"}).status_code

        with ThreadPoolExecutor(max_workers=3) as executor:
            statuses = sorted(executor.map(attempt, range(3)))
        self.assertEqual(statuses, [401, 401, 429])

    def test_ip_limit_multiple_identities_expiry_and_no_enumeration(self):
        client = self.app.test_client()
        messages = []
        for identity in ("missing-one", "missing-two"):
            response = client.post("/api/scanner/login", json={
                "username": identity, "business_slug": "seguro", "password": "wrong",
            }, environ_base={"REMOTE_ADDR": "198.51.100.4"})
            messages.append(response.get_json()["message"])
        limited = client.post("/api/scanner/login", json={
            "username": "cashier", "business_slug": "seguro", "password": "wrong",
        }, environ_base={"REMOTE_ADDR": "198.51.100.4"})
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(messages[0], messages[1])
        expired = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
        AuthRateLimitSlot.query.update({AuthRateLimitSlot.reserved_until: expired})
        db.session.commit()
        self.assertEqual(client.post("/api/scanner/login", json={
            "username": "cashier", "business_slug": "seguro", "password": "wrong",
        }, environ_base={"REMOTE_ADDR": "198.51.100.4"}).status_code, 401)

    def test_readiness_request_ids_and_sanitized_error_logging(self):
        client = self.app.test_client()
        ready = client.get("/api/ready")
        self.assertEqual(ready.status_code, 200)
        self.assertRegex(ready.headers["X-Request-ID"], r"^[0-9a-f]{32}$")
        with patch.object(db.session, "execute", side_effect=OperationalError("SELECT secret", {}, Exception("db"))):
            response = client.get("/api/ready")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"SELECT", response.data)

        self.app.config["PROPAGATE_EXCEPTIONS"] = False
        with self.assertLogs(self.app.logger, level="ERROR") as logs:
            response = client.post("/_test/error?recovery_code=654321", json={
                "password": "private-password", "qr_token": "private-qr-token",
            }, headers={"Authorization": "Bearer secret-token"})
        self.assertEqual(response.status_code, 500)
        joined = " ".join(logs.output)
        for secret in ("secret-token", "private-password", "private-qr-token", "654321"):
            self.assertNotIn(secret, joined)
        self.assertNotIn("Authorization", joined)

    def test_manager_login_form_enforces_csrf(self):
        self.app.config["CSRF_ENABLED"] = True
        client = self.app.test_client()
        page = client.get("/manager/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.get_data(as_text=True)).group(1)
        payload = {"identity": "manager", "business_slug": "seguro", "password": "StaffPassword123!"}
        self.assertEqual(client.post("/manager/login", data=payload).status_code, 400)
        response = client.post("/manager/login", data={**payload, "csrf_token": token})
        self.assertEqual(response.status_code, 302)

    def test_production_csp_local_assets_and_proxy_trust(self):
        env = {"SECRET_KEY": "session-secret-with-more-than-32-characters-a",
               "JWT_SECRET_KEY": "jwt-secret-with-more-than-32-characters-b",
               "CORS_ORIGINS": "https://app.example.test"}
        with patch.dict(os.environ, env, clear=False), \
             patch.object(ProductionConfig, "SQLALCHEMY_DATABASE_URI", self.uri), \
             patch.object(ProductionConfig, "TRUSTED_PROXY_COUNT", 0), \
             patch.object(ProductionConfig, "RECOVERY_MAIL_BACKEND", "smtp"):
            production = create_app("production")
        response = production.test_client().get("/admin/login", headers={"X-Forwarded-For": "203.0.113.99"})
        csp = response.headers["Content-Security-Policy"]
        html = response.get_data(as_text=True)
        self.assertNotIn("unsafe-inline", csp.split("script-src", 1)[1].split(";", 1)[0])
        self.assertIn("script-src-attr 'none'", csp)
        self.assertNotIn("cdn.tailwindcss.com", html)
        self.assertIn("/static/css/tailwind.min.css", html)
        self.assertIn("/static/js/legacy-actions.js", html)
        nonce = re.search(r"nonce=\"([^\"]+)\"", html).group(1)
        self.assertIn(f"'nonce-{nonce}'", csp)
        asset_response = production.test_client().get("/static/css/tailwind.min.css")
        self.assertEqual(asset_response.status_code, 200)
        asset_response.close()
        self.assertTrue(production.config["SESSION_COOKIE_SECURE"])

        with patch.dict(os.environ, env, clear=False), \
             patch.object(ProductionConfig, "SQLALCHEMY_DATABASE_URI", self.uri), \
             patch.object(ProductionConfig, "TRUSTED_PROXY_COUNT", 1), \
             patch.object(ProductionConfig, "RECOVERY_MAIL_BACKEND", "smtp"):
            proxied = create_app("production")
        @proxied.get("/_test/ip")
        def _ip():
            from flask import request
            return {"ip": request.remote_addr, "secure": request.is_secure}
        payload = proxied.test_client().get("/_test/ip", headers={
            "X-Forwarded-For": "203.0.113.8", "X-Forwarded-Proto": "https",
        }).get_json()
        self.assertEqual(payload, {"ip": "203.0.113.8", "secure": True})

    def test_production_rejects_invalid_operational_configuration(self):
        env = {"SECRET_KEY": "session-secret-with-more-than-32-characters-a",
               "JWT_SECRET_KEY": "jwt-secret-with-more-than-32-characters-b",
               "CORS_ORIGINS": "https://app.example.test"}
        cases = (
            ({"RECOVERY_MAIL_BACKEND": "memory"}, "correo en memoria"),
            ({"PUBLIC_BASE_URL": "http://app.example.test"}, "HTTPS"),
            ({"TRUSTED_PROXY_COUNT": 11}, "TRUSTED_PROXY_COUNT"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides), patch.dict(os.environ, env, clear=False), \
                 patch.object(ProductionConfig, "SQLALCHEMY_DATABASE_URI", self.uri), \
                 patch.object(ProductionConfig, "RECOVERY_MAIL_BACKEND", overrides.get("RECOVERY_MAIL_BACKEND", "smtp")), \
                 patch.object(ProductionConfig, "PUBLIC_BASE_URL", overrides.get("PUBLIC_BASE_URL", "")), \
                 patch.object(ProductionConfig, "TRUSTED_PROXY_COUNT", overrides.get("TRUSTED_PROXY_COUNT", 0)):
                with self.assertRaisesRegex(RuntimeError, message):
                    create_app("production")


if __name__ == "__main__":
    unittest.main()
