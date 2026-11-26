import io
from pathlib import Path
import sys
import tempfile
import time
import unittest

from scripts.selfhost_backup import RestoreProcessError, _restore_process, encrypt_stream


class RestoreStreamTests(unittest.TestCase):
    def setUp(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.key = b"k" * 32
        self.archive = Path(work.name) / "encrypted"
        with self.archive.open("wb") as stream:
            encrypt_stream(io.BytesIO(b"synthetic" * 16384), stream, self.key)

    def run_child(self, code, *, timeout=5):
        _restore_process([sys.executable, "-c", code], self.archive, self.key,
                         stage="synthetic", timeout=timeout)

    def test_large_stderr_is_drained_while_archival_input_is_streamed(self):
        self.run_child("import sys; sys.stderr.buffer.write(b'x' * 524288); "
                       "sys.stderr.flush(); sys.stdin.buffer.read()")

    def test_errors_are_classified_without_disclosing_diagnostics(self):
        markers = {
            "extension": 'extension "private-name" is not available',
            "role": 'role "private-name" does not exist',
            "rls_policy": "CREATE POLICY private-name",
            "dependency": 'relation "private-name" does not exist',
            "version": "unsupported version private-name",
            "archive_format": "not a valid archive private-name",
            "space": "No space left on device private-name",
            "memory": "Out of memory private-name",
        }
        for category, message in markers.items():
            with self.subTest(category=category):
                with self.assertRaises(RestoreProcessError) as error:
                    self.run_child(f"import sys; sys.stderr.write({message!r}); sys.exit(2)")
                self.assertEqual(error.exception.category, category)
                self.assertNotIn("private-name", str(error.exception))

    def test_timeout_terminates_only_the_child(self):
        start = time.monotonic()
        with self.assertRaisesRegex(RestoreProcessError, "timeout"):
            self.run_child("import time; time.sleep(30)", timeout=0.3)
        self.assertLess(time.monotonic() - start, 4)

    def test_interrupted_encrypted_stream_fails_even_when_child_exits_zero(self):
        self.archive.write_bytes(b"invalid")
        with self.assertRaisesRegex(RestoreProcessError, "stream"):
            self.run_child("import sys; sys.stdin.buffer.read()")
