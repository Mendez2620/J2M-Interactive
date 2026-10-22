import os
import subprocess
import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text

from app import create_app
from app.extensions import db


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HEAD = "f9a0b1c2d3e4"


class StagingReadinessTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(
            APP_ENV="production",
            SECRET_KEY="local-preflight-session-secret-0123456789",
            JWT_SECRET_KEY="local-preflight-jwt-secret-012345678901",
            REGISTRATION_IDENTITY_HMAC_KEY="local-preflight-identity-secret-0123456",
            REGISTRATION_EMAIL_ENCRYPTION_KEY="local-preflight-email-secret-012345678",
            SQLALCHEMY_DATABASE_URI="postgresql://hidden:hidden@db.invalid/postgres?sslmode=require",
            DATABASE_SSLMODE="require",
            PUBLIC_BASE_URL="https://staging.invalid",
            CORS_ORIGINS="https://staging.invalid",
            SESSION_COOKIE_SECURE=True,
            TRUSTED_PROXY_COUNT=1,
            RECOVERY_MAIL_BACKEND="smtp",
            SMTP_HOST="smtp.invalid",
            SMTP_FROM="no-reply@invalid.test",
            SMTP_USE_TLS=True,
            GOOGLE_WALLET_MODE="disabled",
            UPLOAD_STORAGE_BACKEND="disabled",
            APPLE_PASS_TYPE_ID="",
            APPLE_TEAM_ID="",
            APPLE_CERT_PATH="",
            APPLE_KEY_PATH="",
            APPLE_WWDR_PATH="",
            APPLE_WEB_SERVICE_URL="",
        )
        self.runner = self.app.test_cli_runner()

    def test_preflight_is_read_only_and_never_prints_secrets_or_database_url(self):
        with self.app.app_context():
            db.create_all()
            before = set(db.inspect(db.engine).get_table_names())
        result = self.runner.invoke(args=["staging-preflight", "--json-output"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn('"status": "warning"', result.output)
        for private in (self.app.config["SECRET_KEY"], "hidden:hidden", "db.invalid"):
            self.assertNotIn(private, result.output)
        with self.app.app_context():
            self.assertEqual(set(db.inspect(db.engine).get_table_names()), before)

    def test_preflight_blocks_invalid_configuration_without_external_calls(self):
        self.app.config.update(SECRET_KEY="change-me", CORS_ORIGINS="*", SESSION_COOKIE_SECURE=False)
        result = self.runner.invoke(args=["staging-preflight"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Preflight bloqueado", result.output)
        self.assertNotIn("change-me", result.output)

    def test_explicit_database_check_reads_connectivity_and_revision_only(self):
        with self.app.app_context():
            db.session.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            db.session.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": EXPECTED_HEAD})
            db.session.commit()
        result = self.runner.invoke(args=["staging-preflight", "--check-database"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("revisión Alembic esperada", result.output)
        with self.app.app_context():
            self.assertEqual(db.session.execute(text("SELECT version_num FROM alembic_version")).scalar(), EXPECTED_HEAD)

    def test_destructive_helpers_are_blocked_or_require_explicit_development_confirmation(self):
        environment = os.environ.copy()
        environment.update(FLASK_ENV="development")
        result = subprocess.run(
            [sys.executable, "-m", "flask", "--app", "run.py", "seed-db"],
            cwd=ROOT, env=environment, capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("confirmación explícita", result.stdout + result.stderr)

        production = environment | {
            "FLASK_ENV": "production", "FLASK_DEBUG": "false",
            "SECRET_KEY": "blocked-command-session-secret-0123456789",
            "JWT_SECRET_KEY": "blocked-command-jwt-secret-0123456789012",
            "REGISTRATION_IDENTITY_HMAC_KEY": "blocked-command-identity-secret-012345",
            "REGISTRATION_EMAIL_ENCRYPTION_KEY": "blocked-command-email-secret-01234567",
            "DATABASE_URL": "postgresql://hidden:hidden@db.invalid/postgres",
            "DATABASE_SSLMODE": "require", "PUBLIC_BASE_URL": "https://staging.invalid",
            "CORS_ORIGINS": "https://staging.invalid", "SESSION_COOKIE_SECURE": "true",
            "UPLOAD_STORAGE_BACKEND": "disabled", "GOOGLE_WALLET_MODE": "disabled",
        }
        for command in ("init-db", "seed-db"):
            blocked = subprocess.run(
                [sys.executable, "-m", "flask", "--app", "run.py", command],
                cwd=ROOT, env=production, capture_output=True, text=True,
            )
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("bloqueado en producción", blocked.stdout + blocked.stderr)

    @unittest.skipUnless(os.getenv("TEST_POSTGRES_URL"), "PostgreSQL efímero no disponible")
    def test_alembic_upgrades_ephemeral_postgres_to_single_expected_head(self):
        url = os.environ["TEST_POSTGRES_URL"]
        environment = os.environ.copy()
        environment.update(
            DATABASE_URL=url,
            DATABASE_SSLMODE="disable",
            FLASK_ENV="development",
        )
        result = subprocess.run(
            [sys.executable, "-m", "flask", "--app", "run.py", "db", "upgrade"],
            cwd=ROOT, env=environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        engine = create_engine(url)
        with engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT version_num FROM alembic_version")).scalar(), EXPECTED_HEAD)
            self.assertEqual(connection.execute(text("SELECT COUNT(*) FROM businesses")).scalar(), 0)
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
