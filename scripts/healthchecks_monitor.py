"""Best-effort Healthchecks.io notifications without exposing ping URLs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen


CHECKS = {"database_hourly", "cloud_four_hour", "restore_weekly"}
STATES = {"start", "success", "fail"}
TOKEN_PATH = re.compile(r"/[A-Za-z0-9_-]{20,}")


def _default_secret_file() -> Path:
    configured = os.environ.get("J2M_HEALTHCHECKS_FILE")
    if configured:
        return Path(configured)
    return Path(os.environ.get("LOCALAPPDATA", "")) / "J2M" / "healthchecks.json"


def notify(check_name: str, state: str, secret_file: Path | None = None) -> bool:
    """Send one monitoring signal; failures are deliberately non-fatal."""
    try:
        if check_name not in CHECKS or state not in STATES:
            return False
        path = secret_file or _default_secret_file()
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
        raw_url = settings.get(check_name)
        if not isinstance(raw_url, str) or not raw_url.strip():
            return False
        parsed = urlparse(raw_url)
        synthetic = os.environ.get("J2M_SYNTHETIC_HEALTHCHECK_TEST") == "1"
        clean_url = (
            parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and TOKEN_PATH.fullmatch(parsed.path or "") is not None
        )
        allowed = (
            clean_url
            and parsed.scheme == "https"
            and parsed.hostname in {"hc-ping.com", "healthchecks.io"}
        )
        loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if not allowed and not (synthetic and loopback and clean_url):
            return False
        suffix = {"start": "/start", "success": "", "fail": "/fail"}[state]
        target = raw_url.rstrip("/") + suffix
        request = Request(target, method="POST")
        with urlopen(request, timeout=10) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) == 2:
        notify(args[0], args[1])
    # Monitoring availability never changes the protected operation's result.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
