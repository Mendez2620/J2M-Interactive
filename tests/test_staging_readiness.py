import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, text

from app import create_app
from app.config import normalize_database_url
from app.extensions import db
from app.staging_preflight import _https_endpoint_url


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HEAD = "3d4e5f6a7b8c"


class StagingReadinessTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.app.config.update(
            APP_ENV="production",
            SECRET_KEY="local-preflight-session-secret-0123456789",
            JWT_SECRET_KEY="local-preflight-jwt-secret-012345678901",
            REGISTRATION_IDENTITY_HMAC_KEY="local-preflight-identity-secret-0123456",
            REFERRAL_EVIDENCE_HMAC_KEYS="local-preflight-referral-evidence-key-0123456",
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
            "REFERRAL_EVIDENCE_HMAC_KEYS": "blocked-command-referral-evidence-key-012345",
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

    def test_seed_guard_blocks_direct_function_and_script_on_protected_targets(self):
        import seed

        original = {
            "APP_ENV": seed.app.config.get("APP_ENV"),
            "SQLALCHEMY_DATABASE_URI": seed.app.config.get("SQLALCHEMY_DATABASE_URI"),
        }
        protected = (
            ("production", "sqlite:///:memory:"),
            ("staging", "sqlite:///:memory:"),
            ("testing", "sqlite:///:memory:"),
            ("development", "postgresql+psycopg2://user:secret@remote.invalid/app"),
            ("development", "sqlite:///../outside-project.db"),
        )
        try:
            with patch.object(seed.db, "drop_all") as drop_all:
                for environment, database_url in protected:
                    with self.subTest(environment=environment, database_url=database_url):
                        seed.app.config.update(APP_ENV=environment, SQLALCHEMY_DATABASE_URI=database_url)
                        with self.assertRaises(RuntimeError):
                            seed.reset_and_seed_database(confirmed=True)
                seed.app.config.update(APP_ENV="development", SQLALCHEMY_DATABASE_URI="sqlite:///:memory:")
                with self.assertRaises(RuntimeError):
                    seed.reset_and_seed_database(confirmed=False)
                seed._assert_safe_seed_target(confirmed=True)
                with patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
                    seed._development_seed_password()
                drop_all.assert_not_called()
        finally:
            seed.app.config.update(original)

        environment = os.environ.copy()
        environment.update(
            FLASK_ENV="development",
            DATABASE_URL="postgresql://user:secret@remote.invalid/app",
            DATABASE_SSLMODE="disable",
        )
        direct = subprocess.run(
            [sys.executable, "seed.py", "--yes-i-understand-data-will-be-deleted"],
            cwd=ROOT, env=environment, capture_output=True, text=True,
        )
        self.assertNotEqual(direct.returncode, 0)
        self.assertIn("solo está permitido sobre SQLite local", direct.stdout + direct.stderr)

    def test_apple_wallet_endpoint_accepts_https_paths_only(self):
        for value in (
            "https://api.ejemplo.com/api/apple/v1",
            "https://wallet.example.test:8443/api/apple/v1/",
        ):
            with self.subTest(value=value):
                self.assertTrue(_https_endpoint_url(value))
        for value in (
            "http://api.ejemplo.com/api/apple/v1",
            "https://user:secret@api.ejemplo.com/api/apple/v1",
            "https://api.ejemplo.com/api/apple/v1?token=secret",
            "https://api.ejemplo.com/api/apple/v1#fragment",
            "https://bad_host.example/api/apple/v1",
            "https:///api/apple/v1",
            "not-a-url",
        ):
            with self.subTest(value=value):
                self.assertFalse(_https_endpoint_url(value))

        self.app.config.update(
            APPLE_PASS_TYPE_ID="pass.com.j2m.loyalty",
            APPLE_TEAM_ID="TEAM123456",
            APPLE_CERT_PATH="missing-cert.pem",
            APPLE_KEY_PATH="missing-key.pem",
            APPLE_WWDR_PATH="missing-wwdr.pem",
            APPLE_WEB_SERVICE_URL="https://api.ejemplo.com/api/apple/v1",
        )
        result = self.runner.invoke(args=["staging-preflight", "--json-output"])
        payload = json.loads(result.output.splitlines()[0])
        apple = next(item for item in payload["checks"] if item["check"] == "apple_wallet")
        self.assertEqual(apple["status"], "warning")

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
            [sys.executable, "-m", "flask", "--app", "run.py", "db", "upgrade", "f9a0b1c2d3e4"],
            cwd=ROOT, env=environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        engine = create_engine(
            normalize_database_url(url),
            connect_args={"sslmode": "disable"},
        )
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, web_access_enabled, mobile_access_enabled, created_at) VALUES (1, 'Existente', 'existente', '#000000', '#FFFFFF', 'stamps', 1.0, 10, 0, true, true, false, '2026-01-01')"
            ))
            connection.execute(text(
                "INSERT INTO staff (id, business_id, username, email, password_hash, role, is_active, web_access_enabled, mobile_access_enabled, auth_version, permissions, created_at) VALUES (1, 1, 'existente', 'Mixto@Example.Test', 'hash', 'manager', true, true, false, 1, 'view_stats', '2026-01-01')"
            ))
        result = subprocess.run(
            [sys.executable, "-m", "flask", "--app", "run.py", "db", "upgrade", EXPECTED_HEAD],
            cwd=ROOT, env=environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with engine.connect() as connection:
            self.assertEqual(connection.execute(text("SELECT version_num FROM alembic_version")).scalar(), EXPECTED_HEAD)
            self.assertEqual(
                connection.execute(text("SELECT username, email FROM staff WHERE id = 1")).one(),
                ("existente", "Mixto@Example.Test"),
            )
            index_definition = connection.execute(text(
                "SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND tablename = 'staff' AND indexname = 'ix_staff_email_lower'"
            )).scalar()
            self.assertIsNotNone(index_definition)
            self.assertIn("lower((email)::text)", index_definition.lower())
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO referral_participants (business_id, participant_type, staff_id, invitation_code, created_at, updated_at) VALUES (1, 'staff', 1, 'postgres-direct-default', now(), now())"
            ))
            self.assertIs(
                connection.execute(text(
                    "SELECT is_enabled FROM referral_participants WHERE invitation_code = 'postgres-direct-default'"
                )).scalar(),
                False,
            )
            connection.execute(text(
                "DELETE FROM referral_participants WHERE invitation_code = 'postgres-direct-default'"
            ))
        result = subprocess.run(
            [sys.executable, "-m", "flask", "--app", "run.py", "db", "downgrade", "2c3d4e5f6a7b"],
            cwd=ROOT, env=environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with engine.connect() as connection:
            guard = connection.execute(text(
                "SELECT pg_get_functiondef('validate_referral_incentive_record_subject()'::regprocedure)"
            )).scalar().lower()
            self.assertIn("e.promoter_id = new.beneficiary_id", guard)
            self.assertNotIn("referral_participants", guard)
        result = subprocess.run(
            [sys.executable, "-m", "flask", "--app", "run.py", "db", "upgrade", EXPECTED_HEAD],
            cwd=ROOT, env=environment, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO referral_participants (business_id, participant_type, staff_id, invitation_code, created_at, updated_at) VALUES (1, 'staff', 1, 'blocks-destructive-downgrade', now(), now())"
            ))
        blocked = subprocess.run(
            [sys.executable, "-m", "flask", "--app", "run.py", "db", "downgrade", "2c3d4e5f6a7b"],
            cwd=ROOT, env=environment, capture_output=True, text=True,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("Downgrade bloqueado", blocked.stdout + blocked.stderr)
        with engine.connect() as connection:
            self.assertEqual(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar(),
                EXPECTED_HEAD,
            )
        engine.dispose()


if __name__ == "__main__":
    unittest.main()
