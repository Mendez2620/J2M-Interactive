from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import verify_transition_restore as v


class TransitionRestoreTests(unittest.TestCase):
    def test_authorization_required(self):
        with patch("sys.argv", ["restore", "--index", "x", "--key-file", "x",
                               "--expected-index-sha256", "x",
                               "--expected-legacy-image-id", "x", "--expected-postgres-image-id", "x"]), \
                patch.object(v, "verify_exact_restore") as restore, patch("builtins.print"):
            self.assertEqual(v.main(), 2)
            restore.assert_not_called()

    def test_exact_restore_binds_index_image_and_revision(self):
        index = Path("selfhost-backups/hourly/j2m-set-" + "a" * 32 + ".j2mbackup")
        key = Path("private")
        digest = "b" * 64
        image = "sha256:" + "c" * 64
        evidence = {"backup_id": "a" * 32, "index_sha256": digest}
        report = v.backup.RestoreReport(v.INITIAL_HEAD, 1, 6, 1, 2)
        with patch.object(v, "command", return_value="desktop-linux"), \
                patch.object(v.shutil, "disk_usage", return_value=SimpleNamespace(free=30 * 1024**3)), \
                patch.object(v, "inspect_backup", return_value=evidence), \
                patch.object(v, "image_id", return_value=image), \
                patch.object(v.backup, "restore_integral_isolated", return_value=report) as restore:
            result = v.verify_exact_restore(index, key, digest, image, postgres_image=image)
            self.assertFalse(result["transition_authorized"])
            restore.assert_called_once_with(
                index, key, v.LEGACY_TAG, lock_file=Path("selfhost-backups/.operations.lock"),
                expected_image_id=image, expected_index_sha256=digest, postgres_image=image)
            restore.reset_mock()
            with self.assertRaises(RuntimeError):
                v.verify_exact_restore(index, key, "d" * 64, image, postgres_image=image)
            restore.assert_not_called()
            restore.return_value = v.backup.RestoreReport("unexpected", 1, 6, 1, 2)
            with self.assertRaises(RuntimeError):
                v.verify_exact_restore(index, key, digest, image, postgres_image=image)


    def test_reconstructed_image_requires_synthetic_scope_and_both_pins(self):
        tag = "j2m-interactive:reconstructed-" + v.LEGACY_TAG.rsplit(":", 1)[1]
        with patch.dict(v.os.environ, {"J2M_SYNTHETIC_BACKUP_TEST": "0"}), \
                patch.object(v.backup.subprocess, "run") as docker:
            with self.assertRaises(ValueError):
                v.backup.restore_integral_isolated(Path("unused"), Path("unused"), tag)
            docker.assert_not_called()
        with patch.dict(v.os.environ, {"J2M_SYNTHETIC_BACKUP_TEST": "1"}), \
                patch.object(v.backup.subprocess, "run") as docker:
            with self.assertRaisesRegex(ValueError, "hash de índice"):
                v.backup.restore_integral_isolated(Path("unused"), Path("unused"), tag,
                                                   expected_image_id="sha256:" + "c" * 64)
            docker.assert_not_called()

    def test_rejects_docker_destination_override_before_inspecting(self):
        with patch.dict(v.os.environ, {"DOCKER_HOST": "tcp://example.invalid:2376"}), \
                patch.object(v, "command") as docker:
            with self.assertRaisesRegex(ValueError, "Docker environment override"):
                v.verify_exact_restore(Path("unread"), Path("unread"), "b" * 64, "sha256:" + "c" * 64,
                                       postgres_image="sha256:" + "d" * 64)
            docker.assert_not_called()
