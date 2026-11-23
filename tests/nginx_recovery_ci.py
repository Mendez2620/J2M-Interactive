"""Isolated Docker regression for Nginx upstream recovery during deploy/rollback."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.request import urlopen

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


ROOT = Path(__file__).resolve().parents[1]
NGINX_IMAGE = "nginx@sha256:0563c36f94a6d3ec3584161b77859f740f7e20b4853f52c796a864ce6908e8ea"


def run(*arguments: str, check: bool = True, timeout: int = 60) -> str:
    completed = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=False)
    if check and completed.returncode != 0:
        raise RuntimeError(f"isolated command failed: {arguments[:3]}")
    return completed.stdout.strip()


def write_certificates(directory: Path) -> tuple[Path, Path, Path]:
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "J2M synthetic CI CA")])
    ca = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server = (
        x509.CertificateBuilder().subject_name(server_name).issuer_name(ca_name)
        .public_key(server_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = directory / "ca.crt", directory / "server.crt", directory / "server.key"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(server_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    return ca_path, cert_path, key_path


def backend_config(marker: str, healthy: bool = True) -> str:
    status = 200 if healthy else 503
    return f"""worker_processes 1;
pid /tmp/nginx.pid;
events {{ worker_connections 128; }}
http {{
  access_log off; client_body_temp_path /tmp/client; proxy_temp_path /tmp/proxy;
  fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi;
  server {{ listen 8000;
    location = /api/health {{ return {status} '{marker}'; }}
    location = /api/ready {{ return {status} '{marker}'; }}
    location = /marker {{ return 200 '{marker}'; }}
  }}
}}
"""


def proxy_config() -> str:
    return """worker_processes 1;
pid /tmp/nginx.pid;
events { worker_connections 128; }
http {
  access_log off; client_body_temp_path /tmp/client; proxy_temp_path /tmp/proxy;
  fastcgi_temp_path /tmp/fastcgi; uwsgi_temp_path /tmp/uwsgi; scgi_temp_path /tmp/scgi;
  server { listen 8443 ssl; server_name localhost;
    ssl_certificate /etc/nginx/tls/server.crt;
    ssl_certificate_key /etc/nginx/tls/server.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    location / { proxy_pass http://web:8000; proxy_set_header Host $http_host; }
  }
}
"""


def production_ids() -> dict[str, str]:
    result = {}
    for line in run("docker", "ps", "-a", "--format", "{{.Names}}|{{.ID}}").splitlines():
        name, identifier = line.split("|", 1)
        if name.startswith("j2m-cutover-ready"):
            result[name] = identifier
    return result


def wait_marker(origin: str, context: ssl.SSLContext, expected: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    last_result = "no response"
    while time.monotonic() < deadline:
        try:
            with urlopen(origin + "/marker", context=context, timeout=2) as response:
                body = response.read().decode()
                last_result = f"status={response.status}, body={body!r}"
                if body == expected:
                    return
        except Exception as exc:
            last_result = type(exc).__name__
        time.sleep(0.25)
    raise RuntimeError(f"expected synthetic backend {expected} was not served ({last_result})")


def main() -> int:
    token = uuid.uuid4().hex[:12]
    prefix, network = f"j2m-nginx-ci-{token}", f"j2m-nginx-ci-{token}"
    names = {key: f"{prefix}-{key}" for key in ("web1", "web2", "broken", "filler1", "filler2", "worker", "proxy")}
    before = production_ids()
    run("docker", "pull", NGINX_IMAGE, timeout=180)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        ca_path, _, _ = write_certificates(root)
        (root / "web1.conf").write_text(backend_config("backend-v1"), encoding="utf-8")
        (root / "web2.conf").write_text(backend_config("backend-v2"), encoding="utf-8")
        (root / "broken.conf").write_text(backend_config("broken", healthy=False), encoding="utf-8")
        (root / "proxy.conf").write_text(proxy_config(), encoding="utf-8")
        try:
            run("docker", "network", "create", network)
            def start_backend(name: str, config: str, alias: str | None = None) -> None:
                command = ["docker", "run", "-d", "--name", name, "--network", network, "--read-only", "--tmpfs", "/tmp"]
                if alias:
                    command += ["--network-alias", alias]
                command += ["-v", f"{root / config}:/etc/nginx/nginx.conf:ro", NGINX_IMAGE]
                run(*command)

            start_backend(names["web1"], "web1.conf", "web")
            run("docker", "run", "-d", "--name", names["worker"], "--network", network,
                "--label", f"com.docker.compose.service={prefix}-worker",
                "--label", f"com.docker.compose.project={prefix}", NGINX_IMAGE)
            run("docker", "run", "-d", "--name", names["proxy"], "--network", network,
                "-p", "127.0.0.1::8443", "-v", f"{root / 'proxy.conf'}:/etc/nginx/nginx.conf:ro",
                "-v", f"{root}:/etc/nginx/tls:ro", NGINX_IMAGE)
            port = run("docker", "port", names["proxy"], "8443/tcp").rsplit(":", 1)[1]
            origin = f"https://localhost:{port}"
            context = ssl.create_default_context(cafile=str(ca_path))
            try:
                wait_marker(origin, context, "backend-v1")
            except Exception:
                for diagnostic_name in (names["web1"], names["proxy"]):
                    print(run("docker", "inspect", diagnostic_name, "--format", "{{.State.Status}}/{{.State.ExitCode}}", check=False))
                    print(run("docker", "logs", "--tail", "20", diagnostic_name, check=False))
                raise
            ip1 = json.loads(run("docker", "inspect", names["web1"]))[0]["NetworkSettings"]["Networks"][network]["IPAddress"]

            run("docker", "network", "disconnect", network, names["web1"])
            run("docker", "run", "-d", "--name", names["filler1"], "--network", network, NGINX_IMAGE)
            start_backend(names["web2"], "web2.conf", "web")
            ip2 = json.loads(run("docker", "inspect", names["web2"]))[0]["NetworkSettings"]["Networks"][network]["IPAddress"]
            if ip1 == ip2:
                raise RuntimeError("synthetic replacement did not receive a different IP")
            run(sys.executable, str(ROOT / "scripts" / "selfhost_proxy_recovery.py"),
                "--origin", origin, "--expected-image", NGINX_IMAGE,
                "--web-container", names["web2"], "--worker-service", f"{prefix}-worker",
                "--project", prefix, "--proxy-container", names["proxy"],
                "--attempts", "10", "--retry-seconds", "0.25", "--request-timeout", "2",
                "--ca-file", str(ca_path))
            wait_marker(origin, context, "backend-v2")

            run("docker", "network", "disconnect", network, names["web2"])
            run("docker", "run", "-d", "--name", names["filler2"], "--network", network, NGINX_IMAGE)
            start_backend(names["broken"], "broken.conf", "web")
            failed = subprocess.run([
                sys.executable, str(ROOT / "scripts" / "selfhost_proxy_recovery.py"),
                "--origin", origin, "--expected-image", NGINX_IMAGE,
                "--web-container", names["broken"], "--worker-service", f"{prefix}-worker",
                "--project", prefix, "--proxy-container", names["proxy"],
                "--attempts", "2", "--retry-seconds", "0.1", "--request-timeout", "1",
                "--ca-file", str(ca_path),
            ], capture_output=True, text=True, timeout=30)
            if failed.returncode == 0:
                raise RuntimeError("unhealthy replacement was accepted")
            run("docker", "rm", "-f", names["broken"])
            run("docker", "network", "connect", "--alias", "web", network, names["web1"])
            run(sys.executable, str(ROOT / "scripts" / "selfhost_proxy_recovery.py"),
                "--origin", origin, "--expected-image", NGINX_IMAGE,
                "--web-container", names["web1"], "--worker-service", f"{prefix}-worker",
                "--project", prefix, "--proxy-container", names["proxy"],
                "--attempts", "10", "--retry-seconds", "0.25", "--request-timeout", "2",
                "--ca-file", str(ca_path))
            wait_marker(origin, context, "backend-v1")
        finally:
            for name in names.values():
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)
            subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=30)
    if production_ids() != before:
        raise RuntimeError("isolated test altered an operational J2M container")
    print("Isolated Nginx deploy, backend replacement and rollback verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
