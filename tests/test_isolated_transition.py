"""Exercise the coordinator at every action/journal boundary with a synthetic adapter."""

import json
from pathlib import Path
import tempfile
import unittest

from scripts.isolated_transition import Phase, RehearsalBlocked, rehearse
from scripts.selfhost_backup import exclusive_lock


class Interrupted(BaseException):
    pass


class SyntheticAdapter:
    def __init__(self, interrupt=None):
        self.actions = []
        self.interrupt = interrupt
        self.writer_open = True

    def verify_isolation(self):
        assert self.actions.count(Phase.MIGRATE) <= 1

    def check_prerequisites(self, phase):
        assert tuple(self.actions) == tuple(Phase)[:list(Phase).index(phase)]
        if self.interrupt == (phase, "before"):
            raise Interrupted()
        if phase in (Phase.MIGRATE, Phase.VERIFY, Phase.HTTPS):
            assert not self.writer_open

    def execute(self, phase):
        self.actions.append(phase)
        if phase == Phase.FENCE:
            self.writer_open = False
        if self.interrupt == (phase, "after"):
            raise Interrupted()

    def check_result(self, phase):
        assert self.actions[-1] == phase


class IsolatedTransitionTests(unittest.TestCase):
    def test_success_verifies_all_phases_and_leaves_writers_fenced(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = SyntheticAdapter()
            journal = rehearse(root, adapter)
            record = json.loads(journal.read_text())
            self.assertEqual(record["history"][-1], {"phase": "https", "status": "confirmed"})
            self.assertEqual(len(record["history"]), 2 * len(Phase))
            self.assertFalse(adapter.writer_open)
            self.assertEqual(adapter.actions, list(Phase))

    def test_interruptions_never_replay_pending_operations(self):
        for phase in Phase:
            for when in ("before", "after"):
                with self.subTest(phase=phase, when=when), tempfile.TemporaryDirectory() as root:
                    adapter = SyntheticAdapter((phase, when))
                    with self.assertRaises(Interrupted):
                        rehearse(root, adapter)
                    journal = Path(root, "transition.json")
                    self.assertEqual(json.loads(journal.read_text())["history"][-1],
                                     {"phase": phase.value, "status": "pending"})
                    journal.write_text('{"phase":"https","status":"confirmed"}')
                    with self.assertRaises(RehearsalBlocked):
                        rehearse(root, adapter)
                    self.assertEqual(adapter.actions.count(phase), int(when == "after"))
                    self.assertLessEqual(adapter.actions.count(Phase.MIGRATE), 1)

    def test_concurrent_rehearsal_cannot_start(self):
        with tempfile.TemporaryDirectory() as root:
            adapter = SyntheticAdapter()
            with exclusive_lock(Path(root, "transition.lock")):
                with self.assertRaises(RuntimeError):
                    rehearse(root, adapter)
            self.assertEqual(adapter.actions, [])
            self.assertFalse(Path(root, "transition.json").exists())

    def test_connection_error_stops_before_migration(self):
        class Disconnected(SyntheticAdapter):
            def check_result(self, phase):
                super().check_result(phase)
                if phase == Phase.FENCE:
                    raise ConnectionError("Synthetic connection lost")

        with tempfile.TemporaryDirectory() as root:
            adapter = Disconnected()
            with self.assertRaises(ConnectionError):
                rehearse(root, adapter)
            journal = json.loads(Path(root, "transition.json").read_text())
            self.assertEqual(journal["history"][-1], {"phase": "fence", "status": "blocked"})
            self.assertNotIn(Phase.MIGRATE, adapter.actions)
            self.assertFalse(adapter.writer_open)
