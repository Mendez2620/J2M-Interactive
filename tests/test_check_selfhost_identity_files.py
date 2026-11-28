import tempfile
import unittest
from pathlib import Path

from scripts.check_selfhost_identity_files import CONTAINER_CA, ROLES, validate_identity_files


HOST = "aws-0-example.pooler.supabase.com"
PROJECT = "a" * 20


class SelfHostIdentityFilesTests(unittest.TestCase):
    def setUp(self):
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.paths = {role: Path(self.root, role) for role in ROLES}
        for role, path in self.paths.items():
            ca = "system" if role == "backup" else CONTAINER_CA
            path.write_text(
                f"DATABASE_URL=postgresql://j2m_{role}.{PROJECT}:synthetic-{role}@{HOST}:5432"
                f"/postgres?sslmode=verify-full&sslrootcert={ca}\n"
                + ("GOOGLE_WALLET_MODE=disabled\nAPPLE_WALLET_MODE=disabled\n" if role != "backup" else ""),
                encoding="utf-8",
            )

    def validate(self):
        validate_identity_files(self.paths, HOST, PROJECT)

    def replace(self, role, previous, replacement):
        path = self.paths[role]
        path.write_text(path.read_text().replace(previous, replacement), encoding="utf-8")

    def test_dedicated_files_are_accepted(self):
        self.validate()

    def test_wrong_role_password_reuse_and_same_file_are_rejected(self):
        self.replace("worker", "synthetic-worker", "synthetic-web")
        with self.assertRaises(ValueError):
            self.validate()
        self.replace("worker", "synthetic-web", "synthetic-worker")
        self.replace("worker", "j2m_worker.", "j2m_web.")
        with self.assertRaises(ValueError):
            self.validate()
        self.paths["worker"] = self.paths["web"]
        with self.assertRaises(ValueError):
            self.validate()

    def test_unsafe_tls_or_unexpected_destination_are_rejected(self):
        for replacement in ("require", "verify-full&sslmode=require", "verify-full&host=elsewhere"):
            with self.subTest(replacement=replacement):
                self.replace("web", "sslmode=verify-full", "sslmode=" + replacement)
                with self.assertRaises(ValueError):
                    self.validate()
                self.replace("web", "sslmode=" + replacement, "sslmode=verify-full")
        self.replace("web", HOST, HOST + ".example")
        with self.assertRaises(ValueError):
            self.validate()

    def test_backup_extra_secrets_and_wallet_mode_are_rejected(self):
        self.replace("backup", "\n", "\nSECRET_KEY=synthetic\n")
        with self.assertRaises(ValueError):
            self.validate()
        self.replace("backup", "SECRET_KEY=synthetic\n", "")
        self.replace("web", "GOOGLE_WALLET_MODE=disabled", "GOOGLE_WALLET_MODE=real")
        with self.assertRaises(ValueError):
            self.validate()
