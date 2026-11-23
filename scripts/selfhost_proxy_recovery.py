"""Refresh the local HTTPS proxy after an approved web-container replacement."""

from __future__ import annotations

import argparse
import json
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen


def _docker(*arguments: str, timeout: float = 15.0) -> str:
    completed = subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Docker no pudo verificar o refrescar el proxy HTTPS local.")
    return completed.stdout.strip()


def _validate_origin(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "localhost"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or parsed.port is None
        or not 1 <= parsed.port <= 65535
    ):
        raise ValueError("El origen debe ser HTTPS local, sin credenciales, ruta, consulta ni fragmento.")
    return value.rstrip("/")


def _https_ready(origin: str, context: ssl.SSLContext, timeout: float) -> bool:
    try:
        for path in ("/api/health", "/api/ready"):
            with urlopen(origin + path, timeout=timeout, context=context) as response:
                if response.status != 200:
                    return False
        return True
    except Exception:
        return False


def _verify_runtime(expected_image: str, web_container: str, worker_service: str,
                    project: str, proxy_container: str) -> None:
    web = json.loads(_docker("inspect", web_container))[0]
    if web.get("Config", {}).get("Image") != expected_image or not web.get("State", {}).get("Running"):
        raise RuntimeError("El contenedor web activo no corresponde a la imagen aprobada.")

    worker_ids = [item for item in _docker(
        "ps", "-q", "--filter", f"label=com.docker.compose.service={worker_service}"
    ).splitlines() if item]
    if len(worker_ids) != 1:
        raise RuntimeError("Debe existir exactamente un worker autorizado.")
    worker = json.loads(_docker("inspect", worker_ids[0]))[0]
    labels = worker.get("Config", {}).get("Labels", {}) or {}
    if labels.get("com.docker.compose.project") != project or not worker.get("State", {}).get("Running"):
        raise RuntimeError("El worker activo no pertenece al proyecto autorizado.")

    proxy = json.loads(_docker("inspect", proxy_container))[0]
    if not proxy.get("State", {}).get("Running"):
        raise RuntimeError("El proxy HTTPS local no está en ejecución.")


def recover_proxy(*, origin: str, expected_image: str, web_container: str,
                  worker_service: str, project: str, proxy_container: str,
                  attempts: int = 8, retry_seconds: float = 1.5,
                  request_timeout: float = 5.0, ca_file: Path | None = None) -> bool:
    if not 1 <= attempts <= 20 or not 0 <= retry_seconds <= 10 or not 1 <= request_timeout <= 20:
        raise ValueError("Los límites de recuperación del proxy no son válidos.")
    safe_origin = _validate_origin(origin)
    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    _verify_runtime(expected_image, web_container, worker_service, project, proxy_container)

    if _https_ready(safe_origin, context, request_timeout):
        print("Proxy HTTPS ya resolvía el backend aprobado; no fue necesario recargarlo.")
        return False

    _docker("exec", proxy_container, "nginx", "-t")
    _docker("exec", proxy_container, "nginx", "-s", "reload")
    for attempt in range(attempts):
        if _https_ready(safe_origin, context, request_timeout):
            print("Proxy HTTPS recargado y backend aprobado verificado.")
            return True
        if attempt + 1 < attempts:
            time.sleep(retry_seconds)
    raise RuntimeError("Nginx no alcanzó el backend aprobado dentro del tiempo permitido.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Recupera la resolución del proxy HTTPS local.")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--web-container", default="j2m-cutover-ready-web-1")
    parser.add_argument("--worker-service", default="google-wallet-worker")
    parser.add_argument("--project", default="j2m-cutover-ready")
    parser.add_argument("--proxy-container", default="j2m-cutover-ready-local-https")
    parser.add_argument("--attempts", type=int, default=8)
    parser.add_argument("--retry-seconds", type=float, default=1.5)
    parser.add_argument("--request-timeout", type=float, default=5.0)
    parser.add_argument("--ca-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    recover_proxy(
        origin=args.origin,
        expected_image=args.expected_image,
        web_container=args.web_container,
        worker_service=args.worker_service,
        project=args.project,
        proxy_container=args.proxy_container,
        attempts=args.attempts,
        retry_seconds=args.retry_seconds,
        request_timeout=args.request_timeout,
        ca_file=args.ca_file,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
