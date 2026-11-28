"""Check candidate self-hosted identity files without connecting or printing values."""

import argparse
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, unquote, urlsplit

from scripts.selfhost_transition_preflight import disabled, read_config


ROLES = ("web", "worker", "migrator", "backup")
CONTAINER_CA = "/run/j2m/supabase-ca.pem"
WALLET_PREFIXES = ("GOOGLE_", "APPLE_")
TRANSITION_WALLET_FIELDS = {"GOOGLE_WALLET_MODE", "APPLE_WALLET_MODE",
                            "GOOGLE_WALLET_RECONCILIATION_ENABLED"}


def validate_identity_files(files, expected_host, project_ref):
    if set(files) != set(ROLES) or not re.fullmatch(r"[a-z0-9]{20}", project_ref):
        raise ValueError("Invalid service identity set")
    if not re.fullmatch(r"[a-z0-9.-]+\.pooler\.supabase\.com", expected_host):
        raise ValueError("Invalid pooler destination")
    identities = set()
    passwords = set()
    for role in ROLES:
        path = files[role]
        if path.is_symlink() or not path.is_file():
            raise ValueError("Expected a private regular file per role")
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if identity in identities:
            raise ValueError("Environment file reused across roles")
        identities.add(identity)
        values = read_config(path)
        # Compose format: raw preserves quotes and whitespace that read_config
        # would otherwise normalize. Require identical parsed values for every
        # assignment, not just DATABASE_URL.
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        if any(f"{key}={value}" not in lines or "\x00" in value for key, value in values.items()):
            raise ValueError("Expected unquoted raw identity assignments")
        if any(key.upper().startswith("PG") for key in values):
            raise ValueError("Unexpected PostgreSQL override")
        if role == "backup":
            if set(values) - {"DATABASE_URL", "DATABASE_SSL_ROOT_CERT"}:
                raise ValueError("Unexpected backup environment fields")
            if "DATABASE_SSL_ROOT_CERT" in values:
                ca = Path(values["DATABASE_SSL_ROOT_CERT"])
                if not ca.is_absolute() or ca.is_symlink() or not ca.is_file():
                    raise ValueError("Expected a regular host root certificate")
        elif (not disabled(values) or any(key.startswith(WALLET_PREFIXES) and key not in
                                          TRANSITION_WALLET_FIELDS for key in values)
              or values.get("DATABASE_SSLMODE", "verify-full") != "verify-full"):
            raise ValueError("Unexpected Wallet or TLS configuration in transition files")
        url = urlsplit(values["DATABASE_URL"])
        options = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
        params = dict(options)
        password = unquote(url.password or "")
        ca_path = "system" if role == "backup" else CONTAINER_CA
        if (url.scheme != "postgresql" or url.hostname != expected_host or url.port != 5432
                or url.path != "/postgres" or url.fragment
                or unquote(url.username or "") != f"j2m_{role}.{project_ref}"
                or not password or password in passwords
                or len(params) != len(options) or set(params) != {"sslmode", "sslrootcert"}
                or params["sslmode"] != "verify-full" or params["sslrootcert"] != ca_path):
            raise ValueError("Unexpected destination, role or TLS parameters")
        passwords.add(password)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for role in ROLES:
        parser.add_argument("--" + role, required=True, type=Path)
    parser.add_argument("--expected-host", required=True)
    parser.add_argument("--expected-project-ref", required=True)
    options = parser.parse_args()
    try:
        validate_identity_files({role: getattr(options, role) for role in ROLES},
                                options.expected_host, options.expected_project_ref)
    except (OSError, KeyError, ValueError):
        print(json.dumps({"candidate_files_valid": False, "transition_authorized": False}))
        return 1
    print(json.dumps({"candidate_files_valid": True, "transition_authorized": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
