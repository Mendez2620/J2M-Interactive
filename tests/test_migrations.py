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
}


class MigrationTestCase(unittest.TestCase):
    def run_db_command(self, database_path, command):
        env = os.environ.copy()
        env["DATABASE_URL"] = f"sqlite:///{database_path.as_posix()}"
        env["FLASK_ENV"] = "development"
        result = subprocess.run(
            [sys.executable, "-m", "flask", "--app", "run.py", "db", command],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        return result

    def upgrade(self, database_path):
        result = self.run_db_command(database_path, "upgrade")
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

    def test_upgrade_preserves_existing_business_data(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            path = Path(directory) / "legacy.sqlite"
            engine = create_engine(f"sqlite:///{path.as_posix()}")
            db.metadata.create_all(engine)
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE platform_admins"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN logo_path"))
                connection.execute(text("ALTER TABLE businesses DROP COLUMN cover_path"))
                connection.execute(text("INSERT INTO businesses (id, name, slug, primary_color, secondary_color, loyalty_type, points_per_currency, stamps_reward_limit, cooldown_minutes, is_active, created_at) VALUES (1, 'Negocio existente', 'existente', '#000000', '#FFFFFF', 'stamps', 1.0, 10, 60, 1, '2026-01-01')"))
            engine.dispose()
            self.upgrade(path)
            with sqlite3.connect(path) as connection:
                self.assertEqual(connection.execute("SELECT id, name, slug FROM businesses").fetchall(), [(1, "Negocio existente", "existente")])
                columns = {row[1] for row in connection.execute("PRAGMA table_info(businesses)")}
                self.assertTrue({"logo_path", "cover_path"} <= columns)
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], "9a85c3791398")

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
                self.assertEqual(connection.execute("SELECT version_num FROM alembic_version").fetchone()[0], "9a85c3791398")


if __name__ == "__main__":
    unittest.main()
