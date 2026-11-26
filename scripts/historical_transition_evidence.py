"""Fail-closed evidence primitives for the historical transition rehearsal."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import re
from secrets import token_hex


LEGACY_SHA = "dae140e3684be29c534be3e1725598d226b68e03"
INITIAL_HEAD = "9d0e1f2a3b4c"
FINAL_HEAD = "c3d4e5f6a7b8"
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
TARGET_SHA = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class Backend:
    image: str
    container: str
    address: str

    def __post_init__(self):
        if not IMAGE_ID.fullmatch(self.image) or not CONTAINER_ID.fullmatch(self.container):
            raise ValueError("Invalid immutable Docker identity")
        IPv4Address(self.address)


def prove_https(snapshot, request, access_log, *, revision=None):
    """Require stable Docker identity and one exact access record for each request."""
    backend = snapshot()
    for route in ("health", "ready"):
        nonce = token_hex(24)
        status, observed_revision = request(route, nonce)
        if status != 200 or observed_revision != revision:
            raise RuntimeError("Unexpected HTTPS response or revision")
        matches = [line for line in access_log().splitlines() if line.split()[:1] == [nonce]]
        expected = f"{nonce} /api/{route} {backend.address}:8000 200"
        if matches != [expected]:
            raise RuntimeError("HTTPS could not be bound to the expected upstream")
        if snapshot() != backend:
            raise RuntimeError("Backend identity changed during attestation")
    return backend


class TransitionJournal:
    """Single-use durable journal; interruption never enables replay or continuation."""

    STAGES = ("preflight", "restored", "maintenance", "migrated", "target_verified",
              "failure_injected", "legacy_recovered")

    def __init__(self, path: Path, *, legacy_image, target_image, target_sha, environment):
        if (environment != "synthetic" or legacy_image == target_image
                or not IMAGE_ID.fullmatch(legacy_image) or not IMAGE_ID.fullmatch(target_image)
                or not TARGET_SHA.fullmatch(target_sha) or target_sha == LEGACY_SHA):
            raise ValueError("Unexpected transition resources")
        self.index = 0
        self.file = path.open("x", encoding="utf-8")
        self.record("created", legacy_image=legacy_image, target_image=target_image,
                    target_sha=target_sha, environment=environment,
                    initial_head=INITIAL_HEAD, final_head=FINAL_HEAD)

    def record(self, event, **details):
        self.file.write(json.dumps({"event": event, "time": datetime.now(timezone.utc).isoformat(),
                                    **details}, sort_keys=True) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())

    def advance(self, stage, **details):
        if self.index >= len(self.STAGES) or stage != self.STAGES[self.index]:
            raise RuntimeError("Unexpected transition stage")
        self.record(stage, **details)
        self.index += 1

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        try:
            if error_type is not None:
                self.record("blocked", failure_type=error_type.__name__)
        finally:
            self.file.close()


def verify_record(path, *, legacy_image, target_image, target_sha, now=None):
    """Check provenance fields and completeness; still requires live attestation."""
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    if [row.get("event") for row in rows] != ["created", *TransitionJournal.STAGES]:
        raise ValueError("Incomplete rehearsal")
    expected = {"legacy_image": legacy_image, "target_image": target_image,
                "target_sha": target_sha, "environment": "synthetic",
                "initial_head": INITIAL_HEAD, "final_head": FINAL_HEAD}
    if any(rows[0].get(key) != value for key, value in expected.items()):
        raise ValueError("Rehearsal resources do not match")
    timestamps = [datetime.fromisoformat(row["time"]) for row in rows]
    now = now or datetime.now(timezone.utc)
    if timestamps != sorted(timestamps) or not now - timedelta(hours=24) <= timestamps[0] <= timestamps[-1] <= now:
        raise ValueError("Rehearsal provenance is stale or inconsistent")
