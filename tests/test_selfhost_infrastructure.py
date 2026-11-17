import json
from pathlib import Path
import unittest

from scripts.portable_backup import _portable_text, decrypt_bytes, encrypt_bytes


ROOT = Path(__file__).resolve().parent.parent


class SelfHostInfrastructureTestCase(unittest.TestCase):
    def test_compose_separates_runtime_migration_and_tunnel(self):
        compose = (ROOT / "compose.selfhost.yml").read_text(encoding="utf-8")
        self.assertIn("web:", compose)
        self.assertIn("google-wallet-worker:", compose)
        self.assertIn("migrator:", compose)
        self.assertIn('profiles: ["migrate"]', compose)
        self.assertIn('restart: "no"', compose)
        self.assertIn("127.0.0.1:${J2M_LOCAL_PORT:-8000}:8000", compose)
        self.assertIn("J2M_PROCESS_TYPE: google-wallet-worker", compose)
        self.assertIn("cloudflare/cloudflared:2026.11.0@sha256:", compose)
        self.assertIn('profiles: ["tunnel"]', compose)
        self.assertIn("J2M_CLOUDFLARED_ENV_FILE:-.env.cloudflared", compose)
        self.assertNotIn("TUNNEL_TOKEN", (ROOT / ".env.selfhost.example").read_text(encoding="utf-8"))
        tunnel_env = (ROOT / ".env.cloudflared.example").read_text(encoding="utf-8")
        self.assertIn("TUNNEL_TOKEN=REQUIRED", tunnel_env)
        self.assertNotIn("DATABASE_URL", tunnel_env)
        self.assertNotIn("railway config apply", compose.lower())

    def test_rehearsal_is_isolated_and_wallet_disabled(self):
        overlay = (ROOT / "compose.selfhost.test.yml").read_text(encoding="utf-8")
        test_env = (ROOT / ".env.selfhost.test.example").read_text(encoding="utf-8")
        production_env = (ROOT / ".env.selfhost.example").read_text(encoding="utf-8")
        self.assertIn("postgres:16-alpine", overlay)
        self.assertIn("/var/lib/postgresql/data", overlay)
        self.assertNotIn("ports:", overlay)
        self.assertIn("internal: true", overlay)
        self.assertIn("@postgres-test:5432/j2m_test", test_env)
        for env_text in (test_env, production_env):
            self.assertIn("GOOGLE_WALLET_MODE=disabled", env_text)
            self.assertIn("APPLE_WALLET_MODE=disabled", env_text)
        self.assertNotIn("GOOGLE_SERVICE_ACCOUNT_JSON", test_env)

    def test_approved_release_script_rejects_unapproved_commits_and_migrations(self):
        deploy = (ROOT / "scripts" / "selfhost_deploy_approved.ps1").read_text(encoding="utf-8")
        self.assertIn("$ApprovedSha -ne $mainSha", deploy)
        self.assertIn("$headSha -ne $ApprovedSha", deploy)
        self.assertNotIn("db upgrade", deploy)
        self.assertNotIn("config apply", deploy)

    def test_ci_validates_compose_without_private_values(self):
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("docker compose", workflow)
        self.assertIn("compose.selfhost.yml", workflow)
        self.assertIn("compose.selfhost.test.yml", workflow)
        self.assertIn("-p j2m-pr70-ci", workflow)
        self.assertIn("J2M_CLOUDFLARED_ENV_FILE: .env.cloudflared.example", workflow)
        package_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        self.assertIn("lockfileVersion", package_lock)

    def test_runbook_records_real_isolated_docker_validation(self):
        runbook = (ROOT / "docs" / "local-self-hosting.md").read_text(encoding="utf-8")
        self.assertIn("-p j2m-pr70-test", runbook)
        self.assertIn("PostgreSQL `16.16`", runbook)
        self.assertIn("9d0e1f2a3b4c", runbook)
        self.assertIn("/api/health", runbook)
        self.assertNotIn("Docker Desktop aún no está instalado", runbook)

    def test_private_artifacts_are_excluded_from_git_and_docker(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for pattern in (".env.cloudflared", "*.dpapi", "*.j2mbackup", "selfhost-backups"):
            self.assertIn(pattern, gitignore)
            self.assertIn(pattern, dockerignore)

    def test_portable_backup_encryption_round_trip_and_authentication(self):
        cleartext = b'{"DATABASE_URL":"not-a-real-secret"}'
        password = b"portable-test-password-123"
        payload = encrypt_bytes(cleartext, password)
        self.assertNotIn(cleartext, payload)
        self.assertEqual(decrypt_bytes(payload, password), cleartext)
        with self.assertRaises(Exception):
            decrypt_bytes(payload, b"wrong-password-value")
        self.assertEqual(_portable_text("texto".encode("utf-16-le")), b"texto")


if __name__ == "__main__":
    unittest.main()
