import os
import unittest
from unittest.mock import patch

from app import create_app
from app.config import DevelopmentConfig, ProductionConfig


class ProductionConfigTestCase(unittest.TestCase):
    def valid_environment(self, **overrides):
        environment = {
            "SECRET_KEY": "a-robust-production-secret-key-with-entropy-123",
            "JWT_SECRET_KEY": "another-independent-jwt-key-with-entropy-456",
            "REGISTRATION_IDENTITY_HMAC_KEY": "stable-registration-identity-key-with-entropy-789",
            "REGISTRATION_EMAIL_ENCRYPTION_KEY": "stable-registration-email-encryption-key-456",
            "CORS_ORIGINS": "https://app.example.com",
            "DATABASE_URL": "postgresql://user:password@db.example.test:5432/postgres",
            "DATABASE_SSLMODE": "require",
            "UPLOAD_STORAGE_BACKEND": "disabled",
            "GOOGLE_WALLET_MODE": "disabled",
        }
        environment.update(overrides)
        return environment

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

    def test_production_requires_stable_independent_registration_identity_key(self):
        for identity_key in ("", "short", self.valid_environment()["SECRET_KEY"], self.valid_environment()["JWT_SECRET_KEY"]):
            with self.subTest(identity_key=identity_key), \
                 patch.dict(os.environ, self.valid_environment(REGISTRATION_IDENTITY_HMAC_KEY=identity_key), clear=True), \
                 patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"), \
                 self.assertRaisesRegex(RuntimeError, "REGISTRATION_IDENTITY_HMAC_KEY"):
                create_app("production")

    def test_production_requires_independent_registration_email_encryption_key(self):
        valid = self.valid_environment()
        for encryption_key in ("", "short", valid["SECRET_KEY"], valid["JWT_SECRET_KEY"], valid["REGISTRATION_IDENTITY_HMAC_KEY"]):
            with self.subTest(encryption_key=encryption_key), \
                 patch.dict(os.environ, self.valid_environment(REGISTRATION_EMAIL_ENCRYPTION_KEY=encryption_key), clear=True), \
                 patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"), \
                 self.assertRaisesRegex(RuntimeError, "REGISTRATION_EMAIL_ENCRYPTION_KEY"):
                create_app("production")

    def test_production_forces_debug_off_with_robust_secret(self):
        with patch.dict(os.environ, self.valid_environment(FLASK_DEBUG="true"), clear=True), \
             patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"):
            app = create_app("production")
            self.assertFalse(app.debug)
            client = app.test_client()
            self.assertEqual(client.get("/api/health").status_code, 200)
            self.assertEqual(client.get("/admin/login").status_code, 200)
            self.assertEqual(client.get("/scanner").status_code, 404)
            self.assertIn("max-age=31536000", client.get("/api/health").headers["Strict-Transport-Security"])

    def test_production_requires_explicit_postgresql_database_url(self):
        for database_url, message in ((None, "explícita"), ("sqlite:///ephemeral.db", "SQLite")):
            with self.subTest(database_url=database_url):
                environment = self.valid_environment()
                if database_url is None:
                    environment.pop("DATABASE_URL")
                else:
                    environment["DATABASE_URL"] = database_url
                with patch.dict(os.environ, environment, clear=True), \
                     patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"), \
                     self.assertRaisesRegex(RuntimeError, message):
                    create_app("production")

    def test_production_accepts_explicit_postgresql_formats(self):
        urls = (
            "postgres://user:password@db.example.test:5432/postgres",
            "postgresql://user:password@db.example.test:5432/postgres",
            "postgresql+psycopg2://user:password@db.example.test:5432/postgres",
        )
        for database_url in urls:
            with self.subTest(database_url=database_url), \
                 patch.dict(os.environ, self.valid_environment(DATABASE_URL=database_url), clear=True), \
                 patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"):
                app = create_app("production")
                self.assertTrue(app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql"))
                self.assertEqual(app.config["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"]["sslmode"], "require")

    def test_development_keeps_sqlite_support(self):
        with patch.object(DevelopmentConfig, "SQLALCHEMY_DATABASE_URI", "sqlite:///:memory:"):
            app = create_app("development")
        self.assertEqual(app.config["SQLALCHEMY_DATABASE_URI"], "sqlite:///:memory:")

    def test_production_requires_tls_enforcing_sslmode(self):
        for sslmode in ("disable", "allow", "prefer"):
            with self.subTest(sslmode=sslmode), \
                 patch.dict(os.environ, self.valid_environment(DATABASE_SSLMODE=sslmode), clear=True), \
                 patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"), \
                 self.assertRaisesRegex(RuntimeError, "requiere TLS PostgreSQL"):
                create_app("production")
        environment = self.valid_environment(
            DATABASE_URL="postgresql://user:password@db.example.test/postgres?sslmode=disable"
        )
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"), \
             self.assertRaisesRegex(RuntimeError, "requiere TLS PostgreSQL"):
            create_app("production")

    def test_production_accepts_tls_enforcing_modes(self):
        for sslmode in ("require", "verify-ca", "verify-full"):
            with self.subTest(sslmode=sslmode), \
                 patch.dict(os.environ, self.valid_environment(DATABASE_SSLMODE=sslmode), clear=True), \
                 patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"):
                app = create_app("production")
                self.assertEqual(app.config["SQLALCHEMY_ENGINE_OPTIONS"]["connect_args"]["sslmode"], sslmode)

    def test_production_rejects_local_upload_storage(self):
        with patch.dict(os.environ, self.valid_environment(UPLOAD_STORAGE_BACKEND="local"), clear=True), \
             patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"), \
             self.assertRaisesRegex(RuntimeError, "UPLOAD_STORAGE_BACKEND=disabled"):
            create_app("production")

    def test_production_never_allows_google_wallet_mock_or_incomplete_real_mode(self):
        with patch.dict(os.environ, self.valid_environment(GOOGLE_WALLET_MODE="mock"), clear=True), \
             patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"), \
             self.assertRaisesRegex(RuntimeError, "disabled o real"):
            create_app("production")
        with patch.dict(os.environ, self.valid_environment(
                 GOOGLE_WALLET_MODE="real", GOOGLE_ISSUER_ID="3388000000023203929"
             ), clear=True), \
             patch.object(ProductionConfig, "PUBLIC_BASE_URL", "https://app.example.com"), \
             self.assertRaisesRegex(RuntimeError, "credenciales"):
            create_app("production")


if __name__ == "__main__":
    unittest.main()
