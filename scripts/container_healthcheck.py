"""Container liveness check selected by J2M_PROCESS_TYPE."""

import os
from pathlib import Path
from urllib.request import urlopen


WORKER_COMMANDS = {
    "registration-worker": "run-registration-email-worker",
    "google-wallet-worker": "run-google-wallet-sync-worker",
}


def check_health(process_type, *, commandline=None, http_open=urlopen):
    if process_type == "web":
        port = os.getenv("PORT", "8000")
        with http_open(f"http://127.0.0.1:{port}/api/health", timeout=3) as response:
            return response.status == 200
    expected = WORKER_COMMANDS.get(process_type)
    if not expected:
        return False
    if commandline is None:
        commandline = Path("/proc/1/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    return expected in commandline


if __name__ == "__main__":
    try:
        healthy = check_health(os.getenv("J2M_PROCESS_TYPE", "web").strip().lower())
    except (OSError, ValueError):
        healthy = False
    raise SystemExit(0 if healthy else 1)
