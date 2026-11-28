import json
import unittest
from pathlib import Path

from scripts.check_transition_relation_manifest import verify_relation_manifest


class TransitionRelationManifestTests(unittest.TestCase):
    def setUp(self):
        manifest = Path(__file__).resolve().parents[1] / "scripts/j2m_transition_relation_manifest.json"
        self.manifest = json.loads(manifest.read_text())

    def test_exact_legacy_and_target(self):
        verify_relation_manifest(self.manifest["initial"], self.manifest["legacy"])
        verify_relation_manifest(self.manifest["target"], self.manifest["legacy"] + self.manifest["additions"])

    def test_unknown_view_missing_sequence_and_revision_fail_closed(self):
        for revision, relations in (
            (self.manifest["initial"], self.manifest["legacy"] + ["v:unexpected"]),
            (self.manifest["initial"], self.manifest["legacy"][1:]),
            (self.manifest["target"], self.manifest["legacy"]),
            ("unknown", self.manifest["legacy"]),
        ):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                verify_relation_manifest(revision, relations)
