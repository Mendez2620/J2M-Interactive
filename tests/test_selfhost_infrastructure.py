import json
from pathlib import Path
import unittest


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
        self.assertIn("TUNNEL_TOKEN: ${CLOUDFLARE_TUNNEL_TOKEN:-}", compose)
        self.assertIn("CLOUDFLARE_TUNNEL_TOKEN=REQUIRED", (ROOT / ".env.selfhost.example").read_text(encoding="utf-8"))
        self.assertNotIn("railway config apply", compose.lower())

    def test_rehearsal_is_isolated_and_wallet_disabled(self):
        overlay = (ROOT / "compose.selfhost.test.yml").read_text(encoding="utf-8")
        test_env = (ROOT / ".env.selfhost.test.example").read_text(encoding="utf-8")
        production_env = (ROOT / ".env.selfhost.example").read_text(encoding="utf-8")
        self.assertIn("postgres:16-alpine", overlay)
        self.assertIn("/var/lib/postgresql/data", overlay)
        self.assertNotIn("ports:", overlay)
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
        package_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        self.assertIn("lockfileVersion", package_lock)


if __name__ == "__main__":
    unittest.main()
