"""Observational preflight for the one-time historical transition.

No network/database connections, Docker mutations, pin changes, or migration.
Success of individual observations never authorizes the transition.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import ssl
import subprocess
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from scripts import selfhost_backup as backup
from scripts.historical_transition_evidence import Backend, IMAGE_ID, LEGACY_SHA, TARGET_SHA, prove_https

PROJECT = "j2m-cutover-ready"
NETWORK = PROJECT + "_j2m-private"
LEGACY_TAG = "j2m-interactive:" + LEGACY_SHA
SERVICES = {"web": PROJECT + "-web-1",
            "google-wallet-worker": PROJECT + "-google-wallet-worker-1",
            "proxy": PROJECT + "-local-https"}
BACKUP_NAME = re.compile(r"j2m-set-([0-9a-f]{32})\.j2mbackup")


def command(*args):
    proc = subprocess.run(args, check=False, capture_output=True, text=True, timeout=30)
    if proc.returncode:
        raise RuntimeError("Local inspection failed")
    return proc.stdout.strip()


def read_config(path):
    values = {}
    if path.is_symlink() or not path.is_file():
        raise ValueError("Expected private regular environment file")
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, val = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in values:
            raise ValueError("Ambiguous environment file")
        val = val.strip()
        if val.startswith(('"', "'")):
            if len(val) < 2 or val[-1] != val[0]:
                raise ValueError("Unclosed environment value")
            val = val[1:-1]
        values[key] = val
    return values


def disabled(values):
    return (values.get("GOOGLE_WALLET_MODE") == "disabled"
            and values.get("APPLE_WALLET_MODE") == "disabled"
            and values.get("GOOGLE_WALLET_RECONCILIATION_ENABLED", "false") == "false")


def file_identity(path):
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return None
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns


def inspect_backup(index, key_file, *, now=None, max_age_seconds=5400):
    """Authenticate the exact DB/media set; timestamp comes from encrypted index."""
    match = BACKUP_NAME.fullmatch(index.name)
    if not match or index.is_symlink() or not index.is_file():
        raise ValueError("Expected an integral index")
    before = backup._hash_file(index)
    key = backup.load_dpapi_key(key_file)
    data = backup._decrypt_json_bundle(index, key)
    if data.get("backup_id") != match[1] or data.get("consistency") != "database-first-append-only-media":
        raise ValueError("Unexpected integral index identity")
    created = datetime.fromisoformat(data["created_at"])
    now = now or datetime.now(timezone.utc)
    if created.tzinfo is None or not 0 <= (now - created).total_seconds() <= max_age_seconds:
        raise ValueError("Integral backup timestamp is not recent")
    components = data["components"]
    if not isinstance(components, list) or len(components) != 2:
        raise ValueError("Expected exactly two components")
    names = set()
    fingerprints = {}
    for component in components:
        name = component["filename"]
        if not isinstance(name, str) or Path(name).name != name or name in names:
            raise ValueError("Unexpected component name")
        path = index.parent / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Expected regular backup component")
        names.add(name)
        for artifact in (path, backup._manifest(path), backup._sidecar(path)):
            if artifact.is_symlink():
                raise ValueError("Unexpected linked backup artifact")
            fingerprints[artifact] = file_identity(artifact)
    if (not backup.verify_integral_set(index, key_file) or backup._hash_file(index) != before
            or any(file_identity(path) != identity for path, identity in fingerprints.items())):
        raise RuntimeError("Integral backup authentication failed or index changed")
    return {"backup_id": match[1], "index_sha256": before,
            "created_at": created.astimezone(timezone.utc).isoformat(),
            "component_count": 2, "restore_revision": None}


def newest_candidate(directory):
    candidates = [p for p in directory.glob("j2m-set-*.j2mbackup")
                  if BACKUP_NAME.fullmatch(p.name)]
    if not candidates:
        raise ValueError("No integral backup index")
    # A corrupt newest candidate must be reported, not silently hidden.
    return max(candidates, key=lambda p: (p.stat().st_mtime_ns, p.name))


def image_id(tag):
    result = command("docker", "image", "inspect", "--format", "{{.Id}}", tag)
    if not IMAGE_ID.fullmatch(result):
        raise ValueError("Invalid immutable image identity")
    return result


def container_env(item):
    pairs = {}
    for value in item["Config"]["Env"]:
        key, sep, data = value.partition("=")
        if not sep or key in pairs:
            raise ValueError("Ambiguous container environment")
        pairs[key] = data
    return pairs


def loopback_bindings(item, *, required):
    bindings = item["NetworkSettings"].get("Ports") or {}
    actual = [binding for values in bindings.values() if values for binding in values]
    return bool(actual) == required and all(b["HostIp"] == "127.0.0.1" for b in actual)


def inspect_runtime(config, *, include_addresses=False):
    """Observe existing containers only; cannot establish external exclusion."""
    if config.get("J2M_IMAGE") != LEGACY_TAG or not disabled(config):
        raise ValueError("Unexpected operational pin or Wallet mode")
    if not config.get("DATABASE_URL"):
        raise ValueError("Missing private database configuration")
    if command("docker", "context", "show") != "desktop-linux":
        raise ValueError("Expected local Docker Desktop context")
    legacy = image_id(LEGACY_TAG)
    items = json.loads(command("docker", "inspect", *SERVICES.values()))
    by_name = {item["Name"].lstrip("/"): item for item in items}
    if set(by_name) != set(SERVICES.values()):
        raise RuntimeError("Unexpected container identities")
    network, = json.loads(command("docker", "network", "inspect", NETWORK))
    if set(network["Containers"]) != {item["Id"] for item in items}:
        raise RuntimeError("Unexpected operational network membership")
    observed = {}
    for role, name in SERVICES.items():
        item = by_name[name]
        networks = item["NetworkSettings"]["Networks"]
        if not item["State"]["Running"] or set(networks) != {NETWORK}:
            raise RuntimeError("Unexpected container state or network")
        if not loopback_bindings(item, required=(role != "google-wallet-worker")):
            raise RuntimeError("Unexpected published ports")
        if role != "proxy":
            labels = item["Config"]["Labels"]
            values = container_env(item)
            if (item["Image"] != legacy or item["Config"]["Image"] != LEGACY_TAG
                    or labels.get("com.docker.compose.project") != PROJECT
                    or labels.get("com.docker.compose.service") != role
                    or not disabled(values)
                    or values.get("DATABASE_URL") != config["DATABASE_URL"]):
                raise RuntimeError("Unexpected application identity or configuration")
        if not IMAGE_ID.fullmatch(item["Image"]) or not networks[NETWORK].get("IPAddress"):
            raise RuntimeError("Missing immutable image or network address")
        observed[role] = {"container_id": item["Id"], "image_id": item["Image"]}
        if include_addresses:
            observed[role]["address"] = networks[NETWORK]["IPAddress"]
    workers = command("docker", "ps", "-q", "--no-trunc", "--filter",
                      "label=com.docker.compose.service=google-wallet-worker").splitlines()
    if workers != [observed["google-wallet-worker"]["container_id"]]:
        raise RuntimeError("Unexpected local worker membership")
    if image_id(LEGACY_TAG) != legacy:
        raise RuntimeError("Legacy tag changed")
    return observed


class RejectRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def attest_legacy_https(config):
    """Correlate loopback HTTPS with logs from the same inspected proxy instance."""
    expected = inspect_runtime(config, include_addresses=True)
    web = expected["web"]
    proxy = expected["proxy"]["container_id"]
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl.create_default_context()),
                         RejectRedirect())

    def snapshot():
        if inspect_runtime(config, include_addresses=True) != expected:
            raise RuntimeError("Docker identities changed during attestation")
        return Backend(web["image_id"], web["container_id"], web["address"])

    def request(route, nonce):
        url = f"https://localhost:8443/api/{route}"
        with opener.open(Request(url, headers={"X-J2M-Probe": nonce}), timeout=5) as response:
            if response.geturl() != url:
                raise RuntimeError("Unexpected HTTPS destination")
            return response.status, response.headers.get("X-J2M-Revision")

    prove_https(snapshot, request, lambda: command("docker", "logs", "--since", "60s", proxy))
    return {"https_routes": 2, "legacy_backend_correlated": True}


def inspect_checkout(approved_sha):
    if not TARGET_SHA.fullmatch(approved_sha) or approved_sha == LEGACY_SHA:
        raise ValueError("Invalid target SHA")
    if command("git", "status", "--porcelain"):
        raise RuntimeError("Checkout is not clean")
    if command("git", "rev-parse", "HEAD") != approved_sha or command(
            "git", "rev-parse", "origin/main") != approved_sha:
        raise RuntimeError("Target does not match local HEAD and cached origin/main")
    return {"sha": approved_sha, "remote_verified": False}


def observation(action):
    try:
        return {"status": "observed", "evidence": action()}
    except Exception as exc:
        # Never include exception text, environment values, paths or raw Docker output.
        return {"status": "blocked", "reason": type(exc).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--approved-sha", required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env.selfhost"))
    parser.add_argument("--backup-dir", type=Path, default=Path("selfhost-backups/hourly"))
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--attest-legacy-https", action="store_true")
    args = parser.parse_args()
    report = {
        "checkout": observation(lambda: inspect_checkout(args.approved_sha)),
        "runtime": observation(lambda: inspect_runtime(read_config(args.env_file))),
        "target_image": observation(lambda: {"image_id": image_id(
            "j2m-interactive:" + args.approved_sha)} if TARGET_SHA.fullmatch(args.approved_sha)
            else None),
        "integral_backup": observation(lambda: inspect_backup(
            newest_candidate(args.backup_dir), args.key_file)),
        "disk": observation(lambda: {"free_bytes": shutil.disk_usage(args.backup_dir).free}),
        "remaining": ["live HTTPS attestation", "exact isolated DB and media restore",
                      "operational Alembic and data verification", "external writer exclusion",
                      "scheduled task coordination", "pooler affinity", "transition authorization"],
        "transition_authorized": False,
    }
    if args.attest_legacy_https:
        report["legacy_https"] = observation(lambda: attest_legacy_https(read_config(args.env_file)))
    print(json.dumps(report, indent=2))
    # This command is inventory only. Even complete observations are not GO.
    required = ["checkout", "runtime", "target_image", "integral_backup", "disk"]
    if args.attest_legacy_https:
        required.append("legacy_https")
    return 2 if all(report[key]["status"] == "observed" for key in required) else 1


if __name__ == "__main__":
    raise SystemExit(main())
