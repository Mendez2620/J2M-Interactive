import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from sqlalchemy import create_engine, text

from app.extensions import db
from app import models  # noqa: F401 - register complete model metadata


ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TABLES = {
    "alembic_version", "apple_devices", "business_categories", "businesses",
    "customers", "platform_admins", "staff", "transactions",
    "staff_sessions",
    "access_audits",
    "customer_recovery_challenges", "customer_recovery_authorizations",
    "customer_recovery_rate_limits",
    "loyalty_ledger_entries",
    "auth_rate_limit_slots",
    "customer_registration_challenges", "customer_registration_identity_claims",
    "registration_email_jobs",
    "promoters", "promoter_sessions", "referral_attributions",
    "referral_eligibilities", "referral_eligibility_decisions",
    "referral_incentive_rules", "referral_incentive_rule_audits",
    "referral_incentive_records",
    "referral_participants", "referral_participant_audits", "referral_program_audits",
    "customer_linking_enrollments", "customer_credentials", "customer_sessions",
    "customer_auth_challenges", "customer_auth_audits",
}


class MigrationTestCase(unittest.TestCase):
    def run_db_command(self, database_path, command, target=None):
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
        env["FLASK_ENV"] = "development"
        args = [sys.executable, "-m", "flask", "--app", "run.py", "db", command]
        if target:
            args.append(target)
        result = subprocess.run(
            args,
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        return result

    def upgrade(self, database_path, target=None):
        result = self.run_db_command(database_path, "upgrade", target)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_upgrade_creates_complete_schema_on_empty_database(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "fresh.sqlite"
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue(EXPECTED_TABLES <= tables)
                columns = {row[1] for row in connection.execute("PRAGMA table_info(businesses)")}
                self.assertTrue({"logo_path", "cover_path", "visual_theme", "font_style", "text_size"} <= columns)
                self.assertTrue({"wallet_program_name", "wallet_text_color", "wallet_preset"} <= columns)
                tx_columns = {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}
                self.assertIn("request_fingerprint", tx_columns)
                email_job_columns = {row[1]: row for row in connection.execute("PRAGMA table_info(registration_email_jobs)")}
                self.assertTrue({"challenge_id", "encrypted_code", "status", "attempts", "next_attempt_at", "lease_token"} <= email_job_columns.keys())
                self.assertEqual(email_job_columns["encrypted_code"][3], 0)
                referral_fks = connection.execute("PRAGMA foreign_key_list(referral_attributions)").fetchall()
                self.assertGreaterEqual(sum(1 for row in referral_fks if row[2] == "promoters"), 2)
                self.assertGreaterEqual(sum(1 for row in referral_fks if row[2] == "customers"), 2)
                eligibility_fks = connection.execute("PRAGMA foreign_key_list(referral_eligibilities)").fetchall()
                decision_fks = connection.execute("PRAGMA foreign_key_list(referral_eligibility_decisions)").fetchall()
                self.assertIn("RESTRICT", {row[6] for row in eligibility_fks if row[2] == "referral_attributions"})
                self.assertIn("RESTRICT", {row[6] for row in decision_fks if row[2] == "referral_eligibilities"})
                triggers = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )}
                self.assertTrue({
                    "trg_referral_decisions_no_update", "trg_referral_decisions_no_delete",
                    "trg_referral_rule_audits_no_update", "trg_referral_rule_audits_no_delete",
                    "trg_referral_incentive_records_no_update", "trg_referral_incentive_records_no_delete",
                    "trg_referral_participant_audits_no_update", "trg_referral_participant_audits_no_delete",
                    "trg_referral_program_audits_no_update", "trg_referral_program_audits_no_delete",
                    "trg_customer_auth_audits_no_update", "trg_customer_auth_audits_no_delete",
                } <= triggers)
                self.assertEqual(connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0], "5f6a7b8c9d0e")
                default_sql = {
                    row[1]: row[4]
                    for row in connection.execute("PRAGMA table_info(referral_participants)")
                }
                self.assertIn(str(default_sql["is_enabled"]).strip("()'\""), {"0", "false", "FALSE"})
                connection.execute(
                    "INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, web_access_enabled, mobile_access_enabled, created_at) VALUES (1, 'Default', 'default', '#000000', '#FFFFFF', 'points', 1, 10, 0, 1, 1, 0, '2026-01-03')"
                )
                connection.execute(
                    "INSERT INTO staff (id, business_id, username, password_hash, role, is_active, web_access_enabled, mobile_access_enabled, auth_version, permissions, created_at) VALUES (1, 1, 'staff-default', 'hash', 'cashier', 1, 1, 0, 0, '', '2026-01-03')"
                )
                connection.execute(
                    "INSERT INTO referral_participants (business_id, participant_type, staff_id, invitation_code, created_at, updated_at) VALUES (1, 'staff', 1, 'direct-sql-disabled', '2026-01-03', '2026-01-03')"
                )
                self.assertEqual(connection.execute(
                    "SELECT is_enabled FROM referral_participants WHERE staff_id = 1"
                ).fetchone()[0], 0)

    def test_upgrade_preserves_existing_business_data(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "legacy.sqlite"
            engine = create_engine(f"sqlite:///{path.as_posix()}")
            db.metadata.create_all(engine)
            with engine.begin() as connection:
                for table in (
                    "customer_auth_audits", "customer_sessions", "customer_auth_challenges",
                    "customer_credentials", "customer_linking_enrollments",
                ):
                    connection.execute(text(f"DROP TABLE {table}"))
                connection.execute(text("DROP TABLE platform_admins"))
                connection.execute(text("DROP TABLE referral_program_audits"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN logo_path"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN cover_path"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN web_access_enabled"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN mobile_access_enabled"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN visual_theme"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN font_style"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN text_size"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN referrals_enabled"))
                for column in (
                    "wallet_program_name", "wallet_card_text", "wallet_text_color",
                    "wallet_balance_label", "wallet_instructions", "wallet_contact",
                    "wallet_preset",
                ):
                    connection.execute(text(f"ALTER TABLE businesses DROP COLUMN {column}"))
                connection.execute(text("ALTER TABLE staff DROP COLUMN web_access_enabled"))
                connection.execute(text("ALTER TABLE staff DROP COLUMN mobile_access_enabled"))
                connection.execute(text("ALTER TABLE staff DROP COLUMN auth_version"))
                connection.execute(text("ALTER TABLE staff DROP COLUMN permissions"))
                connection.execute(text("DROP TABLE staff_sessions"))
                connection.execute(text("INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, created_at) VALUES (1, 'Negocio existente', 'existente', '#000000', '#FFFFFF', 'stamps', 1.0, 10, 60, 1, '2026-01-01')"))
            engine.dispose()
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT id, name, slug FROM businesses").fetchall(), [(1, "Negocio existente", "existente")])
                columns = {row[1] for row in connection.execute("PRAGMA table_info(businesses)")}
                self.assertTrue({"logo_path", "cover_path"} <= columns)
                self.assertIn("referrals_enabled", columns)
                self.assertEqual(connection.execute("SELECT referrals_enabled FROM businesses WHERE id = 1").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], "5f6a7b8c9d0e")
                self.assertIn("ix_staff_email_lower", {row[1] for row in connection.execute("PRAGMA index_list(staff)")})
                self.assertEqual(connection.execute("SELECT web_access_enabled, mobile_access_enabled FROM businesses WHERE id = 1").fetchone(), (0, 0))
                self.assertEqual(connection.execute("SELECT visual_theme, font_style, text_size FROM businesses WHERE id = 1").fetchone(), ("modern", "modern", "standard"))

    def test_referral_program_backfill_only_enables_existing_active_participants(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "referral-program.sqlite"
            self.upgrade(path, "3d4e5f6a7b8c")
            with sqlite3.connect(path) as connection:
                for business_id, slug in ((1, "active"), (2, "unused")):
                    connection.execute(
                        "INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, web_access_enabled, mobile_access_enabled, created_at) VALUES (?, ?, ?, '#000000', '#FFFFFF', 'points', 1, 10, 0, 1, 1, 0, '2026-01-03')",
                        (business_id, slug.title(), slug),
                    )
                connection.execute("INSERT INTO staff (id, business_id, username, password_hash, role, is_active, web_access_enabled, mobile_access_enabled, auth_version, permissions, created_at) VALUES (1, 1, 'staff', 'hash', 'cashier', 1, 1, 0, 0, 'refer_customers', '2026-01-03')")
                connection.execute("INSERT INTO referral_participants (business_id, participant_type, staff_id, invitation_code, is_enabled, created_at, updated_at) VALUES (1, 'staff', 1, 'active-code', 1, '2026-01-03', '2026-01-03')")
                connection.commit()
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT id, referrals_enabled FROM businesses ORDER BY id").fetchall(), [(1, 1), (2, 0)])

    def test_downgrade_is_blocked_before_destroying_fresh_schema(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "downgrade.sqlite"
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                connection.execute("INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, created_at) VALUES (1, 'Conservar', 'conservar', '#000000', '#FFFFFF', 'stamps', 1.0, 10, 60, 1, '2026-01-01')")
            reversible_customer_auth = self.run_db_command(path, "downgrade")
            self.assertEqual(
                reversible_customer_auth.returncode, 0,
                reversible_customer_auth.stdout + reversible_customer_auth.stderr,
            )
            reversible = self.run_db_command(path, "downgrade")
            self.assertEqual(reversible.returncode, 0, reversible.stdout + reversible.stderr)
            reversible_index = self.run_db_command(path, "downgrade")
            self.assertEqual(reversible_index.returncode, 0, reversible_index.stdout + reversible_index.stderr)
            reversible_eligibility = self.run_db_command(path, "downgrade")
            self.assertEqual(
                reversible_eligibility.returncode, 0,
                reversible_eligibility.stdout + reversible_eligibility.stderr,
            )
            reversible_participant = self.run_db_command(path, "downgrade")
            self.assertEqual(
                reversible_participant.returncode, 0,
                reversible_participant.stdout + reversible_participant.stderr,
            )
            reversible_staff_index = self.run_db_command(path, "downgrade")
            self.assertEqual(
                reversible_staff_index.returncode, 0,
                reversible_staff_index.stdout + reversible_staff_index.stderr,
            )
            result = self.run_db_command(path, "downgrade")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Downgrade bloqueado", result.stdout + result.stderr)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT name FROM businesses WHERE id = 1").fetchone()[0], "Conservar")
                columns = {row[1] for row in connection.execute("PRAGMA table_info(businesses)")}
                self.assertTrue({"logo_path", "cover_path"} <= columns)
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], "f9a0b1c2d3e4")

    def test_staff_email_index_upgrade_and_downgrade_preserve_existing_staff(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "staff-email-index.sqlite"
            self.upgrade(path, "f9a0b1c2d3e4")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, web_access_enabled, mobile_access_enabled, created_at) VALUES (1, 'Existente', 'existente', '#000000', '#FFFFFF', 'stamps', 1.0, 10, 0, 1, 1, 0, '2026-01-01')"
                )
                connection.execute(
                    "INSERT INTO staff (id, business_id, username, email, password_hash, role, is_active, web_access_enabled, mobile_access_enabled, auth_version, permissions, created_at) VALUES (1, 1, 'existente', 'Mixto@Example.Test', 'hash', 'manager', 1, 1, 0, 1, 'view_stats', '2026-01-01')"
                )
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual(
                    connection.execute("SELECT username, email FROM staff WHERE id = 1").fetchone(),
                    ("existente", "Mixto@Example.Test"),
                )
                index_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'ix_staff_email_lower'"
                ).fetchone()[0].lower()
                self.assertIn("lower(email)", index_sql)
            result = self.run_db_command(path, "downgrade", "f9a0b1c2d3e4")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            with sqlite3.connect(path) as connection:
                self.assertEqual(
                    connection.execute("SELECT username, email FROM staff WHERE id = 1").fetchone(),
                    ("existente", "Mixto@Example.Test"),
                )
                self.assertIsNone(connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'ix_staff_email_lower'"
                ).fetchone())

    def test_referral_eligibility_upgrade_backfills_historical_attributions_as_pending(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "referral-eligibility.sqlite"
            self.upgrade(path, "0a1b2c3d4e5f")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, web_access_enabled, mobile_access_enabled, created_at) VALUES (1, 'Uno', 'uno', '#000000', '#FFFFFF', 'stamps', 1.0, 10, 0, 1, 1, 0, '2026-01-01')"
                )
                connection.execute(
                    "INSERT INTO customers (id, business_id, full_name, current_points, current_stamps, qr_code_token, created_at) VALUES (1, 1, 'Cliente', 0, 0, 'qr', '2026-01-01')"
                )
                connection.execute(
                    "INSERT INTO promoters (id, business_id, name, username, password_hash, referral_code, is_active, auth_version, created_at) VALUES (1, 1, 'Promotor', 'promotor', 'hash', 'ref', 1, 0, '2026-01-01')"
                )
                connection.execute(
                    "INSERT INTO referral_attributions (id, business_id, promoter_id, customer_id, created_at) VALUES (1, 1, 1, 1, '2026-01-01')"
                )
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute(
                    "SELECT business_id, attribution_id, promoter_id, customer_id, status FROM referral_eligibilities"
                ).fetchall(), [(1, 1, 1, 1, "pending")])

    def test_referral_eligibility_upgrade_rejects_incomplete_preexisting_tables(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "incomplete-eligibility.sqlite"
            self.upgrade(path, "0a1b2c3d4e5f")
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE referral_eligibilities (id INTEGER PRIMARY KEY)")
                connection.execute("CREATE TABLE referral_eligibility_decisions (id INTEGER PRIMARY KEY)")
            result = self.run_db_command(path, "upgrade")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Esquema de elegibilidad previo incompatible", result.stdout + result.stderr)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0], "0a1b2c3d4e5f")

    def test_idempotency_migration_preserves_rows_and_scopes_keys_by_business(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "idempotency.sqlite"
            self.upgrade(path, "c4f1a2b3d4e5")
            with sqlite3.connect(path) as connection:
                for business_id in (1, 2):
                    connection.execute(
                        "INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, web_access_enabled, mobile_access_enabled, created_at) VALUES (?, ?, ?, '#000000', '#FFFFFF', 'stamps', 1.0, 10, 0, 1, 0, 0, '2026-01-01')",
                        (business_id, f"Negocio {business_id}", f"negocio-{business_id}"),
                    )
                    connection.execute(
                        "INSERT INTO customers (id, business_id, full_name, current_points, current_stamps, qr_code_token, created_at) VALUES (?, ?, ?, 0, 0, ?, '2026-01-01')",
                        (business_id, business_id, f"Cliente {business_id}", f"token-{business_id}"),
                    )
                    connection.execute(
                        "INSERT INTO transactions (id, business_id, customer_id, type, points_amount, stamps_amount, purchase_amount, request_id, timestamp) VALUES (?, ?, ?, 'earn', 0, 1, 0, ?, '2026-01-01')",
                        (business_id, business_id, business_id, f"legacy-{business_id}"),
                    )
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 2)
                self.assertTrue(all(row[0] is None for row in connection.execute(
                    "SELECT request_fingerprint FROM transactions").fetchall()))
                connection.execute(
                    "INSERT INTO transactions (business_id, customer_id, type, points_amount, stamps_amount, purchase_amount, request_id, request_fingerprint, timestamp) VALUES (1, 1, 'earn', 0, 1, 0, 'shared-key', 'a', '2026-01-02')"
                )
                connection.execute(
                    "INSERT INTO transactions (business_id, customer_id, type, points_amount, stamps_amount, purchase_amount, request_id, request_fingerprint, timestamp) VALUES (2, 2, 'earn', 0, 1, 0, 'shared-key', 'b', '2026-01-02')"
                )

    def test_access_audit_migration_preserves_existing_permissions_and_users(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "access-audit.sqlite"
            self.upgrade(path, "d5e6f7a8b9c0")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, web_access_enabled, mobile_access_enabled, created_at) VALUES (1, 'Conservar', 'conservar', '#000000', '#FFFFFF', 'stamps', 1.0, 10, 0, 1, 1, 1, '2026-01-01')"
                )
                connection.execute(
                    "INSERT INTO staff (id, business_id, username, password_hash, role, is_active, web_access_enabled, mobile_access_enabled, auth_version, permissions, created_at) VALUES (1, 1, 'gerente', 'hash', 'manager', 1, 1, 0, 3, 'view_stats,manage_cashiers', '2026-01-01')"
                )
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                self.assertIn("access_audits", {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")})
                self.assertEqual(connection.execute("SELECT username, auth_version, permissions FROM staff WHERE id = 1").fetchone(), ("gerente", 3, "view_stats,manage_cashiers"))
                audit_columns = {row[1]: row[3] for row in connection.execute("PRAGMA table_info(access_audits)")}
                self.assertEqual(audit_columns["affected_staff_id"], 0)
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], "5f6a7b8c9d0e")

    def test_referral_incentive_migration_is_incremental_and_history_blocks_downgrade(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "referral-incentives.sqlite"
            self.upgrade(path, "1b2c3d4e5f6a")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, web_access_enabled, mobile_access_enabled, created_at) VALUES (1, 'Conservar', 'conservar-8c2', '#000000', '#FFFFFF', 'points', 1.0, 10, 0, 1, 1, 0, '2026-01-01')"
                )
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute(
                    "SELECT name, loyalty_type FROM businesses WHERE id = 1"
                ).fetchone(), ("Conservar", "points"))
                record_columns = {
                    row[1] for row in connection.execute(
                        "PRAGMA table_info(referral_incentive_records)"
                    )
                }
                self.assertTrue({
                    "attribution_id", "source_transaction_id", "rule_version"
                } <= record_columns)
                triggers = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger'"
                    )
                }
                self.assertTrue({
                    "trg_referral_incentive_record_subject",
                    "trg_referral_incentive_records_no_update",
                    "trg_referral_incentive_records_no_delete",
                } <= triggers)
                connection.execute(
                    "INSERT INTO referral_incentive_rules (id, business_id, beneficiary_type, loyalty_program, reward_type, reward_amount, eligible_condition, status, version, created_by_type, created_by_id, updated_by_type, updated_by_id, created_at, updated_at) VALUES (1, 1, 'external_promoter', 'points', 'points', 5, 'first_verified_purchase', 'draft', 1, 'staff', 1, 'staff', 1, '2026-01-01', '2026-01-01')"
                )
                connection.execute(
                    "INSERT INTO referral_incentive_rule_audits (business_id, rule_id, rule_version, action, actor_type, actor_id, new_snapshot, reason, request_id, request_fingerprint, created_at) VALUES (1, 1, 1, 'created', 'staff', 1, '{}', 'Conservar', 'request-1', 'fingerprint', '2026-01-01')"
                )
            result = self.run_db_command(path, "downgrade", "1b2c3d4e5f6a")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Downgrade bloqueado", result.stdout + result.stderr)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM referral_incentive_rule_audits"
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0], "2c3d4e5f6a7b")

    def test_referral_participant_upgrade_backfills_historical_promoters(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "referral-participants.sqlite"
            self.upgrade(path, "2c3d4e5f6a7b")
            with sqlite3.connect(path) as connection:
                connection.execute("INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, created_at) VALUES (1, 'Negocio', 'negocio', '#000000', '#FFFFFF', 'points', 1, 10, 60, 1, '2026-01-01')")
                connection.execute("INSERT INTO customers (id, business_id, full_name, phone, current_points, current_stamps, qr_code_token, created_at) VALUES (1, 1, 'Cliente', '+525500000001', 0, 0, 'qr-customer', '2026-01-01')")
                connection.execute("INSERT INTO promoters (id, business_id, name, username, password_hash, referral_code, is_active, auth_version, created_at) VALUES (1, 1, 'Promotor', 'promotor', 'hash', 'legacy-referral-code', 1, 0, '2026-01-01')")
                connection.execute("INSERT INTO referral_attributions (id, business_id, promoter_id, customer_id, created_at) VALUES (1, 1, 1, 1, '2026-01-02')")
                connection.execute("INSERT INTO referral_eligibilities (id, business_id, attribution_id, promoter_id, customer_id, status, created_at, updated_at, version) VALUES (1, 1, 1, 1, 1, 'pending', '2026-01-02', '2026-01-02', 0)")
                connection.commit()
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                participant = connection.execute(
                    "SELECT business_id, participant_type, promoter_id, invitation_code, is_enabled FROM referral_participants"
                ).fetchone()
                self.assertEqual(participant, (1, "external_promoter", 1, "legacy-referral-code", 1))
                participant_id = connection.execute(
                    "SELECT participant_id FROM referral_attributions WHERE id = 1"
                ).fetchone()[0]
                self.assertEqual(connection.execute(
                    "SELECT participant_id FROM referral_eligibilities WHERE id = 1"
                ).fetchone()[0], participant_id)
                self.assertEqual(connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0], "5f6a7b8c9d0e")

    def test_customer_recovery_migration_preserves_customer_wallet_identity_and_transactions(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "recovery.sqlite"
            self.upgrade(path, "f7a8b9c0d1e2")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, web_access_enabled, mobile_access_enabled, created_at) VALUES (1, 'Conservar', 'conservar', '#000000', '#FFFFFF', 'points', 1.0, 10, 0, 1, 1, 1, '2026-01-01')"
                )
                connection.execute(
                    "INSERT INTO customers (id, business_id, full_name, email, current_points, current_stamps, qr_code_token, created_at) VALUES (1, 1, 'Cliente', 'cliente@example.test', 37, 0, 'qr-inmutable', '2026-01-01')"
                )
                connection.execute(
                    "INSERT INTO transactions (id, business_id, customer_id, type, points_amount, stamps_amount, purchase_amount, request_id, request_fingerprint, timestamp) VALUES (1, 1, 1, 'earn', 37, 0, 37, 'preservar', 'fingerprint', '2026-01-01')"
                )
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT id, current_points, qr_code_token FROM customers").fetchone(), (1, 37.0, "qr-inmutable"))
                self.assertEqual(connection.execute("SELECT customer_id, points_amount FROM transactions").fetchone(), (1, 37.0))
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({
                    "customer_recovery_challenges", "customer_recovery_authorizations",
                    "customer_recovery_rate_limits",
                    "loyalty_ledger_entries",
                } <= tables)
                self.assertEqual(
                    connection.execute("SELECT current_points, current_stamps, qr_code_token FROM customers WHERE id = 1").fetchone(),
                    (37.0, 0, "qr-inmutable"),
                )
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM loyalty_ledger_entries").fetchone()[0], 0)
                consent = connection.execute("SELECT email_verified_at, terms_accepted_at, privacy_accepted_at, marketing_consent FROM customers WHERE id = 1").fetchone()
                self.assertEqual(consent, (None, None, None, 0))

    def test_customer_passkey_migration_is_incremental_and_history_blocks_downgrade(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "customer-passkeys.sqlite"
            self.upgrade(path, "4e5f6a7b8c9d")
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, web_access_enabled, mobile_access_enabled, referrals_enabled, created_at) VALUES (1, 'Conservar', 'passkeys', '#000000', '#FFFFFF', 'points', 1, 10, 0, 1, 1, 0, 0, '2026-01-01')"
                )
                connection.execute(
                    "INSERT INTO customers (id, business_id, full_name, current_points, current_stamps, qr_code_token, created_at) VALUES (1, 1, 'Cliente', 31, 0, 'wallet-stable', '2026-01-01')"
                )
                connection.execute(
                    "INSERT INTO staff (id, business_id, username, password_hash, role, is_active, web_access_enabled, mobile_access_enabled, auth_version, permissions, created_at) VALUES (1, 1, 'staff', 'hash', 'manager', 1, 1, 0, 0, 'authorize_customer_identity', '2026-01-01')"
                )
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({
                    "customer_linking_enrollments", "customer_credentials",
                    "customer_sessions", "customer_auth_challenges", "customer_auth_audits",
                } <= tables)
                self.assertEqual(connection.execute(
                    "SELECT current_points, qr_code_token FROM customers WHERE id=1"
                ).fetchone(), (31.0, "wallet-stable"))
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], "5f6a7b8c9d0e")
            clean = self.run_db_command(path, "downgrade", "4e5f6a7b8c9d")
            self.assertEqual(clean.returncode, 0, clean.stdout + clean.stderr)
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "INSERT INTO customer_auth_audits (business_id, customer_id, action, actor_type, actor_id, reason, created_at) VALUES (1, 1, 'linking_authorized', 'staff', 1, 'Conservar historial', '2026-01-01')"
                )
            blocked = self.run_db_command(path, "downgrade", "4e5f6a7b8c9d")
            self.assertNotEqual(blocked.returncode, 0)
            self.assertIn("Downgrade bloqueado", blocked.stdout + blocked.stderr)


if __name__ == "__main__":
    unittest.main()
