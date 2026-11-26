import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import selfhost_transition_preflight as p


class TransitionPreflightTests(unittest.TestCase):
    def test_encrypted_index_time_and_exact_identity(self):
        now = datetime.now(timezone.utc)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / ("j2m-set-" + "a" * 32 + ".j2mbackup")
            index.write_bytes(b"index")
            for name in ("db", "media"):
                (root / name).write_bytes(b"synthetic")
            payload = {"backup_id": "a" * 32, "created_at": now.isoformat(),
                       "consistency": "database-first-append-only-media",
                       "components": [{"filename": "db"}, {"filename": "media"}]}
            with patch.object(p.backup, "load_dpapi_key", return_value=b"k" * 32), \
                    patch.object(p.backup, "_decrypt_json_bundle", return_value=payload), \
                    patch.object(p.backup, "verify_integral_set", return_value=True):
                result = p.inspect_backup(index, root / "key", now=now)
                self.assertEqual(result["backup_id"], "a" * 32)
                self.assertIsNone(result["restore_revision"])
                payload["created_at"] = (now - timedelta(hours=2)).isoformat()
                with self.assertRaises(ValueError):
                    p.inspect_backup(index, root / "key", now=now)
                payload["created_at"] = now.isoformat()
                payload["backup_id"] = "b" * 32
                with self.assertRaises(ValueError):
                    p.inspect_backup(index, root / "key", now=now)

    def test_rejects_changed_index_and_failed_authentication(self):
        now = datetime.now(timezone.utc)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / ("j2m-set-" + "a" * 32 + ".j2mbackup")
            index.write_bytes(b"index")
            for name in ("db", "media"):
                (root / name).write_bytes(b"synthetic")
            payload = {"backup_id": "a" * 32, "created_at": now.isoformat(),
                       "consistency": "database-first-append-only-media",
                       "components": [{"filename": "db"}, {"filename": "media"}]}
            with patch.object(p.backup, "load_dpapi_key", return_value=b"k" * 32), \
                    patch.object(p.backup, "_decrypt_json_bundle", return_value=payload), \
                    patch.object(p.backup, "verify_integral_set", return_value=False):
                with self.assertRaises(RuntimeError):
                    p.inspect_backup(index, root / "key", now=now)
            def change(*args):
                index.write_bytes(b"changed")
                return True
            with patch.object(p.backup, "load_dpapi_key", return_value=b"k" * 32), \
                    patch.object(p.backup, "_decrypt_json_bundle", return_value=payload), \
                    patch.object(p.backup, "verify_integral_set", side_effect=change):
                with self.assertRaises(RuntimeError):
                    p.inspect_backup(index, root / "key", now=now)

    def test_rejects_ambiguous_config_and_modes(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("GOOGLE_WALLET_MODE=disabled\nGOOGLE_WALLET_MODE=real\n")
            with self.assertRaises(ValueError):
                p.read_config(path)
        self.assertFalse(p.disabled({"GOOGLE_WALLET_MODE": "disabled",
                                     "APPLE_WALLET_MODE": "disabled",
                                     "GOOGLE_WALLET_RECONCILIATION_ENABLED": "true"}))

    def test_errors_sanitized_and_inventory_never_authorizes(self):
        self.assertEqual(p.observation(lambda: (_ for _ in ()).throw(
            RuntimeError("private secret"))), {"status": "blocked", "reason": "RuntimeError"})
        with patch.object(p, "observation", return_value={"status": "observed"}), \
                patch("sys.argv", ["preflight", "--approved-sha", "a" * 40,
                                   "--key-file", "private"]), patch("builtins.print") as output:
            self.assertEqual(p.main(), 2)
            self.assertFalse(json.loads(output.call_args.args[0])["transition_authorized"])
