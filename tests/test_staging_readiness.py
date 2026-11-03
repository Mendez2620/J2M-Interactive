import json
import os
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

from sqlalchemy import create_engine, text

from app import create_app
from app.config import normalize_database_url
from app.extensions import db
from app.staging_preflight import _https_endpoint_url
from app.services.google_wallet_sync_service import (
    _ADVISORY_NAMESPACE, _CLASS_ADVISORY_NAMESPACE, _postgres_session_lock,
)


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_HEAD = "9d0e1f2a3b4c"


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
            APPLE_WALLET_MODE="disabled",
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
            APPLE_WALLET_MODE="real",
            APPLE_PASS_AUTH_KEYS="staging-test-apple-pass-auth-key-at-least-32-bytes",
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
            protected = connection.execute(text(
                "SELECT relname, relrowsecurity FROM pg_class "
                "WHERE relname = ANY(:tables) ORDER BY relname"
            ), {"tables": [
                "customer_auth_audits", "customer_auth_challenges",
                "customer_credentials", "customer_linking_enrollments",
                "customer_sessions",
            ]}).all()
            self.assertEqual(len(protected), 5)
            self.assertTrue(all(row.relrowsecurity for row in protected))
            wallet_tables = connection.execute(text(
                "SELECT relname, relrowsecurity FROM pg_class "
                "WHERE relname = ANY(:tables) ORDER BY relname"
            ), {"tables": [
                "google_wallet_publication_jobs", "google_wallet_publications",
                "apple_pass_update_sequence", "apple_pass_credentials", "apple_wallet_publications", "apple_wallet_push_jobs",
            ]}).all()
            self.assertEqual(len(wallet_tables), 6)
            self.assertTrue(all(row.relrowsecurity for row in wallet_tables))
            public_grants = connection.execute(text(
                "SELECT COUNT(*) FROM information_schema.table_privileges "
                "WHERE table_schema=current_schema() "
                "AND table_name = ANY(:tables) AND grantee='PUBLIC'"
            ), {"tables": [
                "google_wallet_publication_jobs", "google_wallet_publications",
                "apple_pass_update_sequence", "apple_pass_credentials", "apple_wallet_publications", "apple_wallet_push_jobs",
            ]}).scalar()
            self.assertEqual(public_grants, 0)
            self.assertEqual(connection.execute(text(
                "SELECT COUNT(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
                "WHERE c.relname='customer_auth_audits' "
                "AND t.tgname='trg_customer_auth_audits_append_only' AND NOT t.tgisinternal"
            )).scalar(), 1)
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO google_wallet_publications "
                "(business_id, design_version, execution_number, active_key, provider_mode, status, "
                "created_by_admin_id, reason) VALUES "
                "(1, 1, 1, 'active', 'mock', 'pending', 1, 'claim concurrente')"
            ))
            publication_id = connection.execute(text(
                "SELECT id FROM google_wallet_publications WHERE reason='claim concurrente'"
            )).scalar_one()
        barrier = Barrier(2)

        def claim(token):
            with engine.begin() as connection:
                barrier.wait()
                return connection.execute(text(
                    "UPDATE google_wallet_publications "
                    "SET status='processing', lease_token=:token, locked_at=now() "
                    "WHERE id=:publication_id AND status='pending' AND active_key='active'"
                ), {"token": token, "publication_id": publication_id}).rowcount

        with ThreadPoolExecutor(max_workers=2) as executor:
            claims = list(executor.map(claim, ("worker-a", "worker-b")))
        self.assertEqual(sorted(claims), [0, 1])

        remote_started = Event()
        release_remote = Event()

        def slow_customer_http():
            with _postgres_session_lock(
                engine, _ADVISORY_NAMESPACE, 991,
            ) as acquired:
                self.assertTrue(acquired)
                remote_started.set()
                self.assertTrue(release_remote.wait(5))

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(slow_customer_http)
            self.assertTrue(remote_started.wait(5))
            with _postgres_session_lock(
                engine, _ADVISORY_NAMESPACE, 991,
            ) as balance_worker_acquired:
                self.assertFalse(balance_worker_acquired)
            release_remote.set()
            future.result(timeout=5)
        with _postgres_session_lock(engine, _ADVISORY_NAMESPACE, 991) as acquired_after_http:
            self.assertTrue(acquired_after_http)
        with self.assertRaisesRegex(RuntimeError, "simulated HTTP failure"):
            with _postgres_session_lock(engine, _ADVISORY_NAMESPACE, 992) as acquired:
                self.assertTrue(acquired)
                raise RuntimeError("simulated HTTP failure")
        with _postgres_session_lock(engine, _ADVISORY_NAMESPACE, 992) as acquired_after_error:
            self.assertTrue(acquired_after_error)

        class_started = Event()
        release_class = Event()
        remote_order = []

        def slow_v1_class_http():
            with _postgres_session_lock(
                engine, _CLASS_ADVISORY_NAMESPACE, 1,
            ) as acquired:
                self.assertTrue(acquired)
                remote_order.append("v1-start")
                class_started.set()
                self.assertTrue(release_class.wait(5))
                remote_order.append("v1-finish")

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(slow_v1_class_http)
            self.assertTrue(class_started.wait(5))
            with _postgres_session_lock(
                engine, _CLASS_ADVISORY_NAMESPACE, 1,
            ) as v2_while_v1_acquired:
                self.assertFalse(v2_while_v1_acquired)
            release_class.set()
            future.result(timeout=5)
        with _postgres_session_lock(
            engine, _CLASS_ADVISORY_NAMESPACE, 1,
        ) as v2_after_v1_acquired:
            self.assertTrue(v2_after_v1_acquired)
            remote_order.append("v2")
        self.assertEqual(remote_order, ["v1-start", "v1-finish", "v2"])

        with engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM google_wallet_publications WHERE id=:publication_id"
            ), {"publication_id": publication_id})
            connection.execute(text(
                "INSERT INTO customers (id, business_id, full_name, current_points, current_stamps, qr_code_token, created_at) "
                "VALUES (1, 1, 'Apple PG', 0, 0, 'apple-pg-qr', now())"
            ))
            connection.execute(text(
                "UPDATE apple_pass_update_sequence SET value=1 WHERE id=1"
            ))
            connection.execute(text(
                "INSERT INTO apple_pass_credentials (business_id, customer_id, pass_type_identifier, serial_number, auth_token_digest, update_tag) "
                "VALUES (1, 1, 'pass.com.j2m.test', 'customer_1_pg', repeat('a', 64), 1)"
            ))
            connection.execute(text(
                "INSERT INTO apple_devices (id, customer_id, device_library_identifier, push_token, pass_type_identifier, serial_number, created_at, updated_at) "
                "VALUES (1, 1, 'device-postgres-1', 'push-postgres-1', 'pass.com.j2m.test', 'customer_1_pg', now(), now())"
            ))
            connection.execute(text(
                "INSERT INTO customers (id, business_id, full_name, current_points, current_stamps, qr_code_token, created_at) "
                "VALUES (2, 1, 'Apple PG Two', 0, 0, 'apple-pg-qr-two', now())"
            ))
            connection.execute(text(
                "INSERT INTO apple_wallet_publications (id, business_id, design_version, active_key, status, created_by_admin_id, reason) "
                "VALUES (1, 1, 1, 'active', 'pending', 1, 'claim PostgreSQL')"
            ))
            connection.execute(text(
                "INSERT INTO apple_wallet_push_jobs (publication_id, business_id, customer_id, device_id, design_version, target_update_version, reason) "
                "VALUES (1, 1, 1, 1, 1, 2, 'design')"
            ))
            apple_job_id = connection.execute(text("SELECT id FROM apple_wallet_push_jobs")).scalar_one()
        with self.assertRaises(Exception):
            with engine.begin() as connection:
                connection.execute(text(
                    "INSERT INTO apple_wallet_push_jobs (business_id, customer_id, device_id, design_version, target_update_version, reason) "
                    "VALUES (1, 2, 1, 1, 1, 'balance')"
                ))
        apple_barrier = Barrier(2)

        def claim_apple(token):
            with engine.begin() as connection:
                apple_barrier.wait()
                return connection.execute(text(
                    "UPDATE apple_wallet_push_jobs SET status='processing', lease_token=:token, locked_at=now() "
                    "WHERE id=:job_id AND status='pending'"
                ), {"token": token, "job_id": apple_job_id}).rowcount

        with ThreadPoolExecutor(max_workers=2) as executor:
            apple_claims = list(executor.map(claim_apple, ("apple-a", "apple-b")))
        self.assertEqual(sorted(apple_claims), [0, 1])

        sequence_barrier = Barrier(2)

        def allocate_passkit_marker(_worker):
            sequence_barrier.wait()
            with engine.begin() as connection:
                return connection.execute(text(
                    "UPDATE apple_pass_update_sequence SET value=value+1 WHERE id=1 RETURNING value"
                )).scalar_one()

        with ThreadPoolExecutor(max_workers=2) as executor:
            markers = list(executor.map(allocate_passkit_marker, ("marker-a", "marker-b")))
        self.assertEqual(sorted(markers), [2, 3])
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM apple_wallet_push_jobs"))
            connection.execute(text("DELETE FROM apple_wallet_publications"))
            connection.execute(text("DELETE FROM apple_devices WHERE id=1"))
            connection.execute(text("DELETE FROM apple_pass_credentials"))
            connection.execute(text("DELETE FROM customers WHERE id IN (1, 2)"))
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
