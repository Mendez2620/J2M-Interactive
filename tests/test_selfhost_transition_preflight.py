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

    def test_rejects_changed_component_while_verifying(self):
        now = datetime.now(timezone.utc)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            index = root / ("j2m-set-" + "a" * 32 + ".j2mbackup")
            index.write_bytes(b"index")
            db = root / "db"
            db.write_bytes(b"synthetic")
            (root / "media").write_bytes(b"synthetic")
            payload = {"backup_id": "a" * 32, "created_at": now.isoformat(),
                       "consistency": "database-first-append-only-media",
                       "components": [{"filename": "db"}, {"filename": "media"}]}

            def change(*args):
                db.write_bytes(b"changed component")
                return True

            with patch.object(p.backup, "load_dpapi_key", return_value=b"k" * 32), \
                    patch.object(p.backup, "_decrypt_json_bundle", return_value=payload), \
                    patch.object(p.backup, "verify_integral_set", side_effect=change):
                with self.assertRaises(RuntimeError):
                    p.inspect_backup(index, root / "key", now=now)

    def test_legacy_https_rejects_change_to_the_worker_or_proxy(self):
        base = {role: {"container_id": "a" * 64, "image_id": "sha256:" + "1" * 64,
                       "address": "172.20.0.2"}
                for role in ("web", "google-wallet-worker", "proxy")}
        for role in ("google-wallet-worker", "proxy"):
            with self.subTest(role=role):
                changed = {**base, role: {**base[role], "container_id": "b" * 64}}
                with patch.object(p, "inspect_runtime", side_effect=[base, changed]), \
                        patch.object(p, "build_opener") as opener:
                    with self.assertRaisesRegex(RuntimeError, "identities changed"):
                        p.attest_legacy_https({})
                    opener.return_value.open.assert_not_called()

    def test_rejects_ambiguous_config_and_modes(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "env"
            path.write_text("GOOGLE_WALLET_MODE=disabled\nGOOGLE_WALLET_MODE=real\n")
            with self.assertRaises(ValueError):
                p.read_config(path)
            path.write_text("GOOGLE_WALLET_MODE='disabled\n")
            with self.assertRaisesRegex(ValueError, "Unclosed"):
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


    def test_requested_attestation_participates_in_exit_code(self):
        argv = ["preflight", "--approved-sha", "a" * 40, "--key-file", "private",
                "--attest-legacy-https"]
        for expected, status in ((1, "blocked"), (2, "observed")):
            with self.subTest(status=status), patch("sys.argv", argv), \
                    patch.object(p, "observation", side_effect=[{"status": "observed"}] * 5 + [
                        {"status": status}
                    ]), patch("builtins.print") as output:
                self.assertEqual(p.main(), expected)
                self.assertFalse(json.loads(output.call_args.args[0])["transition_authorized"])

    def test_attestation_rejects_redirect_and_ambiguous_or_missing_logs(self):
        from unittest.mock import MagicMock
        from urllib.error import HTTPError

        base = {role: {"container_id": "a" * 64, "image_id": "sha256:" + "1" * 64,
                       "address": "172.20.0.2"}
                for role in ("web", "google-wallet-worker", "proxy")}
        self.assertIsNone(p.RejectRedirect().redirect_request(None, None, 302, "", {},
                                                               "https://example.invalid"))
        for defect in ("redirect", "missing", "ambiguous", "web_identity"):
            with self.subTest(defect=defect):
                response = MagicMock()
                response.__enter__.return_value = response
                response.status = 200
                response.headers = {}
                response.geturl.return_value = "https://localhost:8443/api/health"
                calls = []

                def open_response(request, **kwargs):
                    nonce = request.get_header("X-j2m-probe")
                    calls.append(f"{nonce} /api/health 172.20.0.2:8000 200")
                    if defect == "redirect":
                        raise HTTPError(request.full_url, 302, "redirect", {}, None)
                    return response

                def inspect(*args, **kwargs):
                    if defect == "web_identity" and calls:
                        return {**base, "web": {**base["web"], "container_id": "b" * 64}}
                    return base

                def logs(*args):
                    if defect == "missing":
                        return ""
                    return "\n".join(calls * (2 if defect == "ambiguous" else 1))

                with patch.object(p, "inspect_runtime", side_effect=inspect), \
                        patch.object(p, "build_opener") as opener, patch.object(p, "command", side_effect=logs):
                    opener.return_value.open.side_effect = open_response
                    with self.assertRaises((RuntimeError, HTTPError)):
                        p.attest_legacy_https({})
