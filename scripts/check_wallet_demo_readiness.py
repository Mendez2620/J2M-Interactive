"""Offline technical preflight for the disabled Wallet Demo environment."""
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.serialization import load_pem_private_key

from app.services.google_wallet import GoogleWalletService

PROJECT = "j2m-wallet-demo"
NETWORK = f"{PROJECT}_synthetic"
DATABASE = "postgresql://j2m_test:j2m-demo-synthetic-only@db:5432/j2m_test"
IMAGE = "j2m-interactive:wallet-demo-synthetic"


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=15).stdout


def valid_candidate(issuer, operational_issuer, origin, credentials):
    if not re.fullmatch(r"[1-9][0-9]*", issuer) or not re.fullmatch(r"[1-9][0-9]*", operational_issuer):
        return False
    try:
        url = urlsplit(origin)
        if (issuer == operational_issuer or url.scheme != "https" or not url.hostname
                or "." not in url.hostname or url.username or url.password or url.query or url.fragment
                or url.path not in ("", "/") or url.port not in (None, 443)):
            return False
        GoogleWalletService._validate_credentials_info(credentials)
        if credentials['token_uri'] != 'https://oauth2.googleapis.com/token':
            return False
        load_pem_private_key(credentials['private_key'].encode(), password=None)
    except (TypeError, ValueError, RuntimeError, AttributeError, KeyError):
        return False
    return True


def runtime():
    network, = json.loads(docker("network", "inspect", NETWORK))
    labels = network.get("Labels") or {}
    if not network.get("Internal") or labels.get("com.docker.compose.project") != PROJECT:
        raise ValueError("Network mismatch")
    members = network.get("Containers") or {}
    containers = json.loads(docker("inspect", *members))
    if len(containers) != 3:
        raise ValueError("Unexpected network members")
    image_id = docker("image", "inspect", "--format", "{{.Id}}", IMAGE).strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise ValueError("Image unavailable")
    found = {}
    for container in containers:
        config = container["Config"]
        labels = config["Labels"]
        role = labels.get("com.docker.compose.service")
        ports = container["HostConfig"].get("PortBindings") or {}
        if (role not in {"db", "app", "worker"} or role in found
                or labels.get("com.docker.compose.project") != PROJECT
                or container["State"].get("Health", {}).get("Status") != "healthy"
                or not container["State"]["Running"] or set(container["NetworkSettings"]["Networks"]) != {NETWORK}):
            raise ValueError("Container mismatch")
        found[role] = container["Id"]
        expected_mounts = {f"{PROJECT}_media"} if role == "app" else set()
        if {m.get("Name") for m in container["Mounts"] if m["Type"] != "tmpfs"} != expected_mounts:
            raise ValueError("Unexpected mounts")
        if role != "app" and ports:
            raise ValueError("Unexpected port")
        if role == "app" and ports != {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18443"}]}:
            raise ValueError("Unexpected app binding")
        if role != "db":
            env = dict(item.split("=", 1) for item in config["Env"])
            expected = {"DATABASE_URL": DATABASE, "GOOGLE_WALLET_MODE": "disabled",
                        "APPLE_WALLET_MODE": "disabled", "GOOGLE_WALLET_RECONCILIATION_ENABLED": "false"}
            if (container["Image"] != image_id or any(env.get(k) != v for k, v in expected.items())
                    or env.get("GOOGLE_SERVICE_ACCOUNT_JSON") or env.get("GOOGLE_ISSUER_ID")):
                raise ValueError("Unexpected app configuration")
    docker("exec", found["app"], "python", "-c",
           "from sqlalchemy import create_engine,text; import os;"
           "from scripts.check_wallet_referral_schema import validate_schema;"
           "c=create_engine(os.environ['DATABASE_URL']).connect();"
           "c.execute(text('SET TRANSACTION READ ONLY'));"
           "raise SystemExit(bool(validate_schema(c)))")


def main():
    try:
        runtime()
        print("Synthetic runtime and schema verified.")
        # The file is inspected locally and its contents are never passed to Docker.
        credentials = json.loads(Path(os.environ["J2M_DEMO_CREDENTIALS_FILE"]).read_text(encoding="utf-8"))
        if not valid_candidate(os.environ["J2M_DEMO_ISSUER"], os.environ["J2M_OPERATIONAL_ISSUER"],
                               os.environ["J2M_DEMO_HTTPS_ORIGIN"], credentials):
            raise ValueError("Invalid candidate")
        print("Candidate issuer, HTTPS origin and credential structure verified.")
    except Exception:
        print("Blocked: technical evidence missing or incompatible. Details suppressed.")
        return 1
    print("Blocked: effective issuer and account permissions, dedicated credentials, test accounts, "
          "HTTPS accessibility and authorized egress require independent verification.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
