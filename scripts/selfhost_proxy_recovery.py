"""Refresh the local HTTPS proxy, verifying the exact release served over TLS."""

from __future__ import annotations

import argparse
import json
import re
import ssl
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen


_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
# The sole deployed version before release identity was introduced.
LEGACY_BOOTSTRAP_SHA = "dae140e3684be29c534be3e1725598d226b68e03"


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


def _https_ready(origin: str, context: ssl.SSLContext, timeout: float,
                 expected_revision: str) -> bool:
    """Never accept 200 from an old or unidentified backend."""
    try:
        for path in ("/api/health", "/api/ready"):
            url = origin + path
            with urlopen(url, timeout=timeout, context=context) as response:
                if (response.status != 200
                        or response.geturl() != url
                        or response.headers.get("X-J2M-Revision") != expected_revision):
                    return False
        return True
    except Exception:
        return False


def _legacy_https_ready(origin: str, context: ssl.SSLContext, timeout: float) -> bool:
    """Legacy-only probe: require TLS, 200/200, no redirects and no revision header."""
    try:
        for path in ("/api/health", "/api/ready"):
            url = origin + path
            with urlopen(url, timeout=timeout, context=context) as response:
                if (response.status != 200 or response.geturl() != url
                        or response.headers.get("X-J2M-Revision")):
                    return False
        return True
    except Exception:
        return False


def _verify_runtime(expected_image: str, web_container: str, worker_service: str,
                    project: str, proxy_container: str, network_name: str) -> None:
    web = json.loads(_docker("inspect", web_container))[0]
    web_labels = web.get("Config", {}).get("Labels", {}) or {}
    web_network = (web.get("NetworkSettings", {}).get("Networks", {}) or {}).get(network_name, {})
    if (
        web.get("Config", {}).get("Image") != expected_image
        or not web.get("State", {}).get("Running")
        or web_labels.get("com.docker.compose.project") != project
        or web_labels.get("com.docker.compose.service") != "web"
        or not web_network.get("IPAddress")
    ):
        raise RuntimeError("Web no corresponde a la imagen, proyecto o red aprobados.")

    worker_ids = [item for item in _docker(
        "ps", "-q", "--filter", f"label=com.docker.compose.service={worker_service}"
    ).splitlines() if item]
    if len(worker_ids) != 1:
        raise RuntimeError("Debe existir exactamente un worker autorizado.")
    worker = json.loads(_docker("inspect", worker_ids[0]))[0]
    labels = worker.get("Config", {}).get("Labels", {}) or {}
    if (labels.get("com.docker.compose.project") != project
            or not worker.get("State", {}).get("Running")):
        raise RuntimeError("El worker activo no pertenece al proyecto autorizado.")

    proxy = json.loads(_docker("inspect", proxy_container))[0]
    proxy_network = (proxy.get("NetworkSettings", {}).get("Networks", {}) or {}).get(network_name, {})
    if not proxy.get("State", {}).get("Running") or not proxy_network.get("IPAddress"):
        raise RuntimeError("El proxy HTTPS no está conectado a la red autorizada.")


def _verify_legacy_web_alias(web_container: str, network_name: str) -> None:
    """Require the Docker web DNS name to resolve uniquely to the inspected web."""
    target = json.loads(_docker("inspect", web_container))[0]
    networks = target.get("NetworkSettings", {}).get("Networks", {}) or {}
    if "web" not in (networks.get(network_name, {}).get("Aliases") or []):
        raise RuntimeError("El rollback legado no tiene alias web autorizado.")
    network = json.loads(_docker("network", "inspect", network_name))[0]
    members = network.get("Containers") or {}
    if sum(data.get("Name") == web_container for data in members.values()) != 1:
        raise RuntimeError("No se pudo identificar el web único del rollback.")
    for container_id, data in members.items():
        if data.get("Name") == web_container:
            continue
        other = json.loads(_docker("inspect", container_id))[0]
        aliases = ((other.get("NetworkSettings", {}).get("Networks", {}) or {})
                   .get(network_name, {}).get("Aliases") or [])
        if "web" in aliases:
            raise RuntimeError("Alias web ambiguo: rollback inseguro.")


def recover_proxy(*, origin: str, expected_image: str, expected_revision: str,
                  web_container: str, worker_service: str, project: str,
                  proxy_container: str, network_name: str | None = None,
                  attempts: int = 8, retry_seconds: float = 1.5,
                  request_timeout: float = 5.0, ca_file: Path | None = None,
                  legacy_bootstrap_rollback: bool = False) -> bool:
    if not 1 <= attempts <= 20 or not 0 <= retry_seconds <= 10 or not 1 <= request_timeout <= 20:
        raise ValueError("Los límites de recuperación del proxy no son válidos.")
    if _REVISION_PATTERN.fullmatch(expected_revision) is None:
        raise ValueError("La revisión esperada debe ser un SHA completo de 40 caracteres.")
    if legacy_bootstrap_rollback and (
        expected_revision != LEGACY_BOOTSTRAP_SHA
        or expected_image != f"j2m-interactive:{LEGACY_BOOTSTRAP_SHA}"
    ):
        raise ValueError("El modo legado solo permite recuperar la versión local anterior aprobada.")
    safe_origin = _validate_origin(origin)
    context = ssl.create_default_context(cafile=str(ca_file) if ca_file else None)
    actual_network = network_name or f"{project}_j2m-private"
    _verify_runtime(expected_image, web_container, worker_service, project, proxy_container,
                    actual_network)

    if legacy_bootstrap_rollback:
        # The currently deployed pre-header build cannot return a revision. A
        # forced validated reload plus unique Docker DNS ownership is required.
        # Never use this exception for deploying a new image.
        _verify_legacy_web_alias(web_container, actual_network)
        _docker("exec", proxy_container, "nginx", "-t")
        _docker("exec", proxy_container, "nginx", "-s", "reload")
        for attempt in range(attempts):
            if _legacy_https_ready(safe_origin, context, request_timeout):
                print("Rollback legado: recarga forzada y HTTPS 200/200; sin identidad SHA por HTTP.")
                return True
            if attempt + 1 < attempts:
                time.sleep(retry_seconds)
        raise RuntimeError("El rollback legado no recuperó HTTPS válido.")

    if _https_ready(safe_origin, context, request_timeout, expected_revision):
        print("Proxy HTTPS ya sirve la revisión aprobada; no fue necesario recargarlo.")
        return False

    _docker("exec", proxy_container, "nginx", "-t")
    _docker("exec", proxy_container, "nginx", "-s", "reload")
    for attempt in range(attempts):
        if _https_ready(safe_origin, context, request_timeout, expected_revision):
            print("Proxy HTTPS recargado y revisión aprobada verificada.")
            return True
        if attempt + 1 < attempts:
            time.sleep(retry_seconds)
    raise RuntimeError("Nginx no alcanzó la revisión aprobada dentro del tiempo permitido.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Recupera la revisión HTTPS del proxy local.")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--expected-image", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--web-container", default="j2m-cutover-ready-web-1")
    parser.add_argument("--worker-service", default="google-wallet-worker")
    parser.add_argument("--project", default="j2m-cutover-ready")
    parser.add_argument("--proxy-container", default="j2m-cutover-ready-local-https")
    parser.add_argument("--network-name")
    parser.add_argument("--legacy-bootstrap-rollback", action="store_true",
                        help="Uso exclusivo del rollback al SHA pre-identidad aprobado.")
    parser.add_argument("--attempts", type=int, default=8)
    parser.add_argument("--retry-seconds", type=float, default=1.5)
    parser.add_argument("--request-timeout", type=float, default=5.0)
    parser.add_argument("--ca-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    recover_proxy(
        origin=args.origin,
        expected_image=args.expected_image,
        expected_revision=args.expected_revision,
        web_container=args.web_container,
        worker_service=args.worker_service,
        project=args.project,
        proxy_container=args.proxy_container,
        network_name=args.network_name,
        attempts=args.attempts,
        retry_seconds=args.retry_seconds,
        request_timeout=args.request_timeout,
        ca_file=args.ca_file,
        legacy_bootstrap_rollback=args.legacy_bootstrap_rollback,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
