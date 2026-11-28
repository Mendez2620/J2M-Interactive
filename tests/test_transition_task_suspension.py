from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest

from postgres_full_schema_fixture import SIMULATED_BACKUP_TASKS, run_simulated_backup_task
from scripts.selfhost_backup import exclusive_lock


class TransitionTaskSuspensionTests(unittest.TestCase):
    def test_suspension_survives_death_and_lock_release(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            lock, marker = root / ".operations.lock", root / "tasks-suspended.json"
            hold = """
from pathlib import Path
import os
import sys
from time import sleep
from scripts.selfhost_backup import exclusive_lock

with exclusive_lock(Path(sys.argv[1])), Path(sys.argv[2]).open("x", encoding="utf-8") as marker:
    marker.write("{}")
    marker.flush()
    os.fsync(marker.fileno())
    sleep(60)
"""
            with subprocess.Popen([sys.executable, "-c", hold, str(lock), str(marker)]) as coordinator:
                try:
                    deadline = monotonic() + 20
                    while not marker.exists() and coordinator.poll() is None and monotonic() < deadline:
                        sleep(.05)
                    self.assertTrue(marker.exists())
                finally:
                    if coordinator.poll() is None:
                        coordinator.kill()
                    coordinator.wait(timeout=8)
            with exclusive_lock(lock):
                self.assertTrue(marker.exists())
            for task in SIMULATED_BACKUP_TASKS:
                self.assertEqual(run_simulated_backup_task(lock, marker, task).returncode, 75)
            marker.unlink()
            for task in SIMULATED_BACKUP_TASKS:
                run_simulated_backup_task(lock, marker, task).check_returncode()
