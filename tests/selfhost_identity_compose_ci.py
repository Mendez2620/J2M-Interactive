"""Check the Compose model with synthetic service-specific values."""

import json
import os
from pathlib import Path
import subprocess
import tempfile


with tempfile.TemporaryDirectory(prefix="j2m-split-compose-") as folder:
    root = Path(folder)
    env = {
        **os.environ, "J2M_IMAGE": "j2m-interactive:synthetic",
        "J2M_ENV_FILE": ".env.selfhost.example",
        "J2M_CLOUDFLARED_ENV_FILE": ".env.cloudflared.example",
    }
    for role in ("web", "worker", "migrator"):
        file = root / role
        file.write_text(f"DATABASE_URL=synthetic-{role}\n", encoding="utf-8")
        env[f"J2M_{role.upper()}_ENV_FILE"] = str(file)
    cert = root / "ca.pem"
    cert.touch()
    env["J2M_DATABASE_CA_FILE"] = str(cert)
    rendered = subprocess.run(
        ["docker", "compose", "-p", "j2m-split-compose-test", "-f", "compose.selfhost.yml",
         "-f", "compose.selfhost.identities.yml", "--profile", "migrate", "config", "--format", "json"],
        env=env, capture_output=True, check=True, timeout=30,
    )
    services = json.loads(rendered.stdout)["services"]
    for role, service in (("web", "web"), ("worker", "google-wallet-worker"), ("migrator", "migrator")):
        configuration = services[service]
        environment = configuration["environment"]
        assert environment["DATABASE_URL"] == f"synthetic-{role}"
        assert "SECRET_KEY" not in environment
        assert any(volume["target"] == "/run/j2m/supabase-ca.pem" and volume["read_only"]
                   for volume in configuration["volumes"])
    assert any(volume["target"] == "/app/instance/media" for volume in services["web"]["volumes"])
print("Compose rendered separate service files and read-only CA mounts.")
