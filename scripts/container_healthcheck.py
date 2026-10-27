"""Container liveness check selected by J2M_PROCESS_TYPE."""

import os
from pathlib import Path
from urllib.request import urlopen


WORKER_COMMANDS = {
    "google-wallet-worker": "run-google-wallet-sync-worker",
}


def _detect_process_type(commandline):
    """Fail closed on ambiguous PID 1; detect legacy Railway services without env."""
    if "run-registration-email-worker" in commandline:
        return None
    matches = [kind for kind, command in WORKER_COMMANDS.items() if command in commandline]
    if len(matches) == 1:
        return matches[0]
    if not matches and "gunicorn" in commandline and "run:app" in commandline:
        return "web"
    return None


def check_health(process_type, *, commandline=None, http_open=urlopen):
    process_type = str(process_type or "").strip().lower()
    if process_type == "auto":
        if commandline is None:
            commandline = Path("/proc/1/cmdline").read_bytes().replace(b"\\0", b" ").decode("utf-8", "replace")
        process_type = _detect_process_type(commandline)
    if not process_type:
        return False
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
        healthy = check_health(os.getenv("J2M_PROCESS_TYPE", "auto").strip().lower())
    except (OSError, ValueError):
        healthy = False
    raise SystemExit(0 if healthy else 1)
