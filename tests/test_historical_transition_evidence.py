import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.historical_transition_evidence import Backend, TransitionJournal, prove_https, verify_record


IMAGES = {"legacy_image": "sha256:" + "1" * 64, "target_image": "sha256:" + "2" * 64,
          "target_sha": "a" * 40}
BACKEND = Backend(IMAGES["legacy_image"], "3" * 64, "172.28.0.5")


class TransitionEvidenceTests(unittest.TestCase):
    def test_two_independent_requests_are_bound_to_the_backend(self):
        rows = []
        nonces = set()

        def request(route, nonce):
            nonces.add(nonce)
            rows.append(f"{nonce} /api/{route} {BACKEND.address}:8000 200")
            return 200, None

        result = prove_https(lambda: BACKEND, request, lambda: "\n".join(rows))
        self.assertEqual(result, BACKEND)
        self.assertEqual(len(nonces), 2)

    def test_rejects_redirect_duplicate_wrong_upstream_and_identity_change(self):
        for defect in ("redirect", "duplicate", "upstream", "identity", "revision"):
            with self.subTest(defect=defect):
                rows = []

                def request(route, nonce):
                    host = "172.28.0.8" if defect == "upstream" else BACKEND.address
                    rows.append(f"{nonce} /api/{route} {host}:8000 200")
                    if defect == "duplicate":
                        rows.append(rows[-1])
                    return (302 if defect == "redirect" else 200,
                            "unexpected" if defect == "revision" else None)

                def snapshot():
                    if rows and defect == "identity":
                        return Backend(BACKEND.image, "9" * 64, BACKEND.address)
                    return BACKEND

                with self.assertRaises(RuntimeError):
                    prove_https(snapshot, request, lambda: "\n".join(rows))

    def test_interruption_refuses_reuse_and_incomplete_evidence(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "evidence.jsonl"
            with self.assertRaisesRegex(RuntimeError, "injected"):
                with TransitionJournal(path, environment="synthetic", **IMAGES) as journal:
                    journal.advance("preflight")
                    raise RuntimeError("injected")
            with self.assertRaises(FileExistsError):
                TransitionJournal(path, environment="synthetic", **IMAGES)
            with self.assertRaises(ValueError):
                verify_record(path, **IMAGES)

    def test_complete_evidence_is_bound_to_images_and_expires(self):
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "evidence.jsonl"
            with TransitionJournal(path, environment="synthetic", **IMAGES) as journal:
                with self.assertRaises(RuntimeError):
                    journal.advance("migrated")
                for stage in journal.STAGES:
                    journal.advance(stage)
            verify_record(path, **IMAGES)
            with self.assertRaises(ValueError):
                verify_record(path, **{**IMAGES, "target_sha": "b" * 40})
            with self.assertRaises(ValueError):
                verify_record(path, **IMAGES, now=datetime.now(timezone.utc) + timedelta(days=2))
