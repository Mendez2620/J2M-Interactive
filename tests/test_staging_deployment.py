import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from werkzeug.datastructures import FileStorage

from app import create_app
from app.routes.admin import persist_images
from scripts.container_healthcheck import check_health


ROOT = Path(__file__).resolve().parent.parent


class StagingDeploymentTestCase(unittest.TestCase):
    def test_postgres_uses_tls_and_conservative_pool_defaults(self):
        env = os.environ.copy()
        env["DATABASE_URL"] = "postgresql://example:secret@pooler.example.test:5432/postgres"
        result = subprocess.run(
            [sys.executable, "-c", "import json; from app.config import Config; print(json.dumps(Config.SQLALCHEMY_ENGINE_OPTIONS))"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        options = json.loads(result.stdout)
        self.assertEqual(options["pool_size"], 3)
        self.assertEqual(options["max_overflow"], 2)
        self.assertTrue(options["pool_pre_ping"])
        self.assertEqual(options["connect_args"]["sslmode"], "require")

    def test_production_uploads_are_disabled_without_persistent_storage(self):
        app = create_app("testing")
        app.config["UPLOAD_STORAGE_BACKEND"] = "disabled"
        upload = FileStorage(stream=io.BytesIO(b"not-written"), filename="logo.png")
        with app.app_context(), self.assertRaisesRegex(RuntimeError, "almacenamiento persistente"):
            persist_images({"logo_file": upload, "cover_file": None})

    def test_railway_build_uses_gunicorn_port_and_readiness(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        railway = (ROOT / "railway.toml").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("npm ci", dockerfile)
        self.assertIn("npm run build:css", dockerfile)
        self.assertIn("gunicorn --bind 0.0.0.0:${PORT:-8000}", dockerfile)
        self.assertNotIn("healthcheckPath", railway)
        self.assertIn("Configure /api/ready únicamente en el servicio web", railway)
        readiness = (ROOT / "docs" / "staging-readiness.md").read_text(encoding="utf-8")
        self.assertIn("ruta `/api/ready`", readiness)
        self.assertIn("servicios worker no deben tener `healthcheckPath`", readiness)
        self.assertIn("Infrastructure as Code", readiness)
        self.assertIn("gunicorn==", requirements)

    def test_container_and_ci_are_hardened_for_staging_validation(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        self.assertIn("USER j2m", dockerfile)
        self.assertIn("HEALTHCHECK", dockerfile)
        self.assertNotIn("db upgrade", dockerfile)
        for pattern in (".env", "certs", "*.pem", "*.key", "*.p12", "*.pfx"):
            self.assertIn(pattern, dockerignore)
        self.assertIn("postgres:16-alpine", workflow)
        self.assertIn("TEST_POSTGRES_URL", workflow)
        self.assertIn("python -m pip check", workflow)
        self.assertNotIn("railway up", workflow.lower())

    def test_container_healthcheck_matches_web_and_each_worker_process(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        self.assertTrue(check_health("web", http_open=lambda *_args, **_kwargs: Response()))
        self.assertTrue(check_health("registration-worker", commandline="flask --app run.py run-registration-email-worker"))
        self.assertTrue(check_health("google-wallet-worker", commandline="flask --app run.py run-google-wallet-sync-worker"))
        self.assertFalse(check_health("registration-worker", commandline="flask --app run.py run-google-wallet-sync-worker"))
        self.assertFalse(check_health("unknown", commandline="anything"))
        self.assertTrue(check_health("auto", commandline="flask --app run.py run-google-wallet-sync-worker"))
        self.assertTrue(check_health("auto", commandline="flask --app run.py run-registration-email-worker"))
        self.assertTrue(check_health("auto", commandline="gunicorn --bind 0.0.0.0:8000 run:app", http_open=lambda *_a, **_k: Response()))
        self.assertFalse(check_health("auto", commandline="unrelated-worker-command"))
        self.assertFalse(check_health("auto", commandline="run-google-wallet-sync-worker run-registration-email-worker"))

        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("python scripts/container_healthcheck.py", dockerfile)


if __name__ == "__main__":
    unittest.main()
