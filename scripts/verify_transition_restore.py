"""Verify one expressly authorized integral restore in isolated Docker resources.

Never called by transition inventory. No production DB connection or migration.
A successful result is an observation, not an authorization or resumable journal.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil

from scripts import selfhost_backup as backup
from scripts.historical_transition_evidence import IMAGE_ID, INITIAL_HEAD
from scripts.selfhost_transition_preflight import LEGACY_TAG, inspect_backup, image_id, command


def verify_exact_restore(index, key_file, expected_hash, expected_image):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or not IMAGE_ID.fullmatch(expected_image):
        raise ValueError("Invalid expected identities")
    if any(os.environ.get(name) for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS",
                                               "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_CONFIG")):
        raise ValueError("Unexpected Docker environment override")
    if command("docker", "context", "show") != "desktop-linux":
        raise ValueError("Expected local Docker Desktop")
    if index.parent.name != "hourly" or index.parent.parent.name != "selfhost-backups":
        raise ValueError("Expected designated local backup directory")
    if index.parent.is_symlink() or index.parent.parent.is_symlink():
        raise ValueError("Unexpected backup directory")
    if shutil.disk_usage(index.parent).free < 20 * 1024**3:
        raise RuntimeError("Insufficient local disk space")
    before = inspect_backup(index, key_file)
    if before["index_sha256"] != expected_hash or image_id(LEGACY_TAG) != expected_image:
        raise RuntimeError("Backup or image does not match approved identity")
    report = backup.restore_integral_isolated(
        index, key_file, LEGACY_TAG,
        lock_file=index.parent.parent / ".operations.lock",
        expected_image_id=expected_image, expected_index_sha256=expected_hash)
    after = inspect_backup(index, key_file)
    if before != after or image_id(LEGACY_TAG) != expected_image or report.alembic_revision != INITIAL_HEAD:
        raise RuntimeError("Restore evidence does not match the historical transition")
    return {"backup_id": before["backup_id"], "index_sha256": expected_hash,
            "image_id": expected_image, "revision": report.alembic_revision,
            "businesses": report.businesses, "customers": report.customers,
            "staff": report.staff, "transactions": report.transactions,
            "ledger_entries": report.ledger_entries, "wallet_sync_jobs": report.wallet_sync_jobs,
            "media_files_verified": report.media_files_verified,
            "transition_authorized": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--expected-index-sha256", required=True)
    parser.add_argument("--expected-legacy-image-id", required=True)
    parser.add_argument("--authorize-isolated-restore", action="store_true")
    args = parser.parse_args()
    if not args.authorize_isolated_restore:
        print("Blocked: separate owner authorization required.")
        return 2
    try:
        result = verify_exact_restore(args.index, args.key_file, args.expected_index_sha256,
                                      args.expected_legacy_image_id)
    except Exception as exc:
        print("Isolated restore not verified (" + type(exc).__name__ + ").")
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
