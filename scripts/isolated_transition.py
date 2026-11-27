"""Coordinate an isolated transition rehearsal without trusting its journal as evidence."""

from enum import Enum
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from scripts.selfhost_backup import exclusive_lock


class Phase(str, Enum):
    PREFLIGHT = "preflight"
    MAINTENANCE = "maintenance"
    FENCE = "fence"
    RESTORE = "restore"
    MIGRATE = "migrate"
    VERIFY = "verify"
    HTTPS = "https"


class RehearsalBlocked(RuntimeError):
    pass


def _save(path, phase, status):
    # Install only a fully flushed revision of the journal.
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump({"phase": phase.value, "status": status}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def rehearse(directory, adapter):
    """Run once. The adapter owns only disposable resources and leaves writers fenced.

    Each step must verify prerequisites against those resources before it acts.
    A failed or interrupted step is never replayed from its journal. The caller
    may discard the disposable rehearsal and start with fresh resources.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    journal = directory / "transition.json"
    with exclusive_lock(directory / "transition.lock"):
        if journal.exists():
            raise RehearsalBlocked("An existing rehearsal requires independent inspection")
        for phase in Phase:
            _save(journal, phase, "pending")
            try:
                adapter.verify_isolation()
                adapter.check_prerequisites(phase)
                adapter.execute(phase)
                adapter.verify_isolation()
                adapter.check_result(phase)
            except Exception:
                _save(journal, phase, "blocked")
                raise
            _save(journal, phase, "confirmed")
        # Reopening is deliberately outside this executor. A successful
        # rehearsal is evidence for review and never an authorization.
        return journal
