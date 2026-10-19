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
        self.assertIn('healthcheckPath = "/api/ready"', railway)
        self.assertIn("gunicorn==", requirements)


if __name__ == "__main__":
    unittest.main()
