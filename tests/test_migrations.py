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
                self.assertTrue({"logo_path", "cover_path"} <= columns)
                tx_columns = {row[1] for row in connection.execute("PRAGMA table_info(transactions)")}
                self.assertIn("request_fingerprint", tx_columns)

    def test_upgrade_preserves_existing_business_data(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "legacy.sqlite"
            engine = create_engine(f"sqlite:///{path.as_posix()}")
            db.metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE platform_admins"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN logo_path"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN cover_path"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN web_access_enabled"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN mobile_access_enabled"))
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
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], "b9c0d1e2f3a4")
                self.assertEqual(connection.execute("SELECT web_access_enabled, mobile_access_enabled FROM businesses WHERE id = 1").fetchone(), (0, 0))

    def test_downgrade_is_blocked_before_destroying_fresh_schema(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "downgrade.sqlite"
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                connection.execute("INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, created_at) VALUES (1, 'Conservar', 'conservar', '#000000', '#FFFFFF', 'stamps', 1.0, 10, 60, 1, '2026-01-01')")
            result = self.run_db_command(path, "downgrade")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Downgrade bloqueado", result.stdout + result.stderr)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT name FROM businesses WHERE id = 1").fetchone()[0], "Conservar")
                columns = {row[1] for row in connection.execute("PRAGMA table_info(businesses)")}
                self.assertTrue({"logo_path", "cover_path"} <= columns)
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], "b9c0d1e2f3a4")

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
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], "b9c0d1e2f3a4")

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
                } <= tables)


if __name__ == "__main__":
    unittest.main()
