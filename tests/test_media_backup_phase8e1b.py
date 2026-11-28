import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from app import create_app
from app.extensions import db
from app.media_storage import LocalMediaStorage
from app.models import Business
from scripts.selfhost_backup import (
    _MediaArchiveReader,
    _MediaArchiveExtractor,
    _complete_backups,
    _encrypted_json_bundle,
    _media_inventory,
    _publish_bundle,
    _restored_media_references,
    _select_backup,
    _hash_file,
    database_backup,
    encrypt_stream,
    restore_isolated,
    media_backup,
    media_backup_from_docker,
    remote_retention_plan,
    restore_media,
    verify_backup,
    verify_integral_set,
)


class MediaBackupPhase8E1BTestCase(unittest.TestCase):
    KEY = b"m" * 32
    BACKUP_ID = "1" * 32

    def test_runner_is_opt_in_and_uses_exact_read_only_operational_volume(self):
        root = Path(__file__).resolve().parent.parent
        runner = (root / "scripts/run_selfhost_backup.ps1").read_text(encoding="utf-8")
        source = (root / "scripts/selfhost_backup.py").read_text(encoding="utf-8")
        example = (root / ".env.selfhost.example").read_text(encoding="utf-8")
        self.assertIn("J2M_MEDIA_BACKUP_ENABLED=false", example)
        self.assertIn('j2m-cutover-ready_j2m-media', runner)
        self.assertIn('dst=/media,readonly', source)
        self.assertIn('"--network", "none"', source)
        with self.assertRaisesRegex(ValueError, "volumen operativo"):
            media_backup_from_docker("j2m-cutover-ready_j2m-media-copy",
                                     "j2m-interactive:" + "f" * 40,
                                     Path("unused"), Path("unused"), self.BACKUP_ID)

    def test_restricted_backup_rejects_other_host_or_network_before_dump(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"J2M_SYNTHETIC_BACKUP_TEST": "1"},
        ), patch("scripts.selfhost_backup.load_dpapi_key", return_value=self.KEY), patch(
            "scripts.selfhost_backup._run_streaming_dump",
        ) as dump:
            root = Path(temporary)
            ca = root / "test-ca"
            ca.write_text("synthetic test placeholder")
            env = root / "backup.env"
            for host, network in (("unexpected", "j2m-roles-abcd1234-internal"), ("db", "bridge")):
                with self.subTest(host=host, network=network):
                    env.write_text(f"DATABASE_URL=postgresql://j2m_backup:test@{host}/full_schema\n"
                                   f"DATABASE_SSL_ROOT_CERT={ca}\n")
                    with self.assertRaises(ValueError):
                        database_backup(env, root / "output", root / "key", network=network,
                                        backup_id=self.BACKUP_ID, retain=False, acquire_lock=False,
                                        restricted_snapshot="00000001-00000002-1")
            dump.assert_not_called()

    def test_legacy_revision_override_requires_synthetic_scope_and_verifier(self):
        for synthetic, verifier in (("0", lambda _: True), ("1", None)):
            with self.subTest(synthetic=synthetic), patch.dict(
                os.environ, {"J2M_SYNTHETIC_BACKUP_TEST": synthetic},
            ):
                with self.assertRaises(ValueError):
                    restore_isolated(Path("unused"), Path("unused"),
                                     certification_head="9d0e1f2a3b4c",
                                     verify_restored_coverage=verifier)

    def _asset(self, root: Path, business: int, kind: str, name: str, content: bytes) -> Path:
        target = root / f"businesses/{business}/{kind}/{name}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def _marker(self, asset: Path, root: Path, business: int, version: int = 2) -> None:
        relative = asset.relative_to(root).as_posix()
        marker = asset.with_name(f".{asset.name}.retained.json")
        marker.write_text(json.dumps({
            "schema": 1, "storage_key": relative, "business_id": business,
            "superseded_by_design_version": version,
            "retired_at": "2026-11-21T00:00:00+00:00",
        }), encoding="utf-8")

    def _backup(self, root: Path, output: Path) -> Path:
        with patch("scripts.selfhost_backup.load_dpapi_key", return_value=self.KEY):
            return media_backup(root, output, output / "key.dpapi", self.BACKUP_ID)

    def test_empty_volume_round_trips_as_authenticated_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media"
            media.mkdir()
            backup = self._backup(media, root / "backups")
            destination = root / "restored"
            with patch("scripts.selfhost_backup.load_dpapi_key", return_value=self.KEY):
                self.assertEqual(restore_media(backup, destination, root / "key", self.BACKUP_ID), [])
            self.assertEqual(list(destination.rglob("*")), [])

    def test_multiple_tenants_current_and_historical_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media"
            old = self._asset(media, 1, "logo", "a" * 32, b"old-logo")
            self._marker(old, media, 1)
            current = self._asset(media, 1, "logo", "b" * 32, b"new-logo")
            second = self._asset(media, 2, "cover", "c" * 32, b"tenant-two")
            backup = self._backup(media, root / "backups")
            destination = root / "restored"
            with patch("scripts.selfhost_backup.load_dpapi_key", return_value=self.KEY):
                restored = restore_media(backup, destination, root / "key", self.BACKUP_ID)
            self.assertEqual(len(restored), 4)
            self.assertEqual((destination / old.relative_to(media)).read_bytes(), b"old-logo")
            self.assertEqual((destination / current.relative_to(media)).read_bytes(), b"new-logo")
            self.assertEqual((destination / second.relative_to(media)).read_bytes(), b"tenant-two")
            app = create_app("testing")
            app.config.update(MEDIA_STORAGE_BACKEND="local", MEDIA_STORAGE_ROOT=str(destination))
            app.extensions["media_storage"] = LocalMediaStorage(destination)
            with app.app_context():
                db.create_all()
                business = Business(id=1, name="Restored", slug="restored",
                                    logo_path=current.relative_to(media).as_posix())
                db.session.add(business)
                db.session.commit()
                client = app.test_client()
                self.assertEqual(client.get(f"/media/{current.relative_to(media).as_posix()}").status_code, 200)
                self.assertEqual(client.get(f"/media/{old.relative_to(media).as_posix()}").status_code, 200)
                db.session.remove()
                db.drop_all()

    def test_tenant_mismatch_and_unmanaged_or_linked_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media"
            asset = self._asset(media, 1, "logo", "a" * 32, b"logo")
            self._marker(asset, media, 2)
            with self.assertRaisesRegex(ValueError, "tenant"):
                _media_inventory(media)
            asset.with_name(f".{asset.name}.retained.json").unlink()
            (media / "secret.txt").write_text("no", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no administrada"):
                _media_inventory(media)
            (media / "secret.txt").unlink()
            linked = self._asset(media, 1, "cover", "d" * 32, b"linked")
            os.link(linked, linked.with_name("e" * 32 + ".png"))
            with self.assertRaisesRegex(ValueError, "enlaces"):
                _media_inventory(media)

    def test_corruption_wrong_set_and_incomplete_copy_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "media"
            self._asset(media, 1, "logo", "a" * 32, b"logo")
            backup = self._backup(media, root / "backups")
            with patch("scripts.selfhost_backup.load_dpapi_key", return_value=self.KEY):
                with self.assertRaisesRegex(ValueError, "conjunto"):
                    restore_media(backup, root / "wrong", root / "key", "2" * 32)
            payload = bytearray(backup.read_bytes())
            payload[-1] ^= 1
            backup.write_bytes(payload)
            self.assertFalse(verify_backup(backup, self.KEY))

    def test_common_manifest_rejects_component_from_another_backup_set(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media_root = root / "media"
            self._asset(media_root, 1, "logo", "a" * 32, b"logo")
            with patch("scripts.selfhost_backup.load_dpapi_key", return_value=self.KEY):
                media_a = media_backup(media_root, root, root / "key", "1" * 32)
                media_b = media_backup(media_root, root, root / "key", "2" * 32)
                database = root / f"j2m-postgres-{'1' * 32}.dump.j2mbackup"
                partial = database.with_name(database.name + ".partial")
                with partial.open("wb") as output:
                    encrypt_stream(io.BytesIO(b"synthetic pg dump"), output, self.KEY)
                _publish_bundle(partial, database, "postgresql", "1" * 32)
                payload = {"schema": 1, "backup_id": "1" * 32,
                           "created_at": "2026-11-21T00:00:00+00:00",
                           "consistency": "database-first-append-only-media",
                           "components": [
                               {"kind": "postgresql", "filename": database.name,
                                "sha256": _hash_file(database), "size": database.stat().st_size},
                               {"kind": "media", "filename": media_b.name,
                                "sha256": _hash_file(media_b), "size": media_b.stat().st_size},
                           ]}
                index = root / f"j2m-set-{'1' * 32}.j2mbackup"
                _encrypted_json_bundle(payload, index, self.KEY, "integral-set", "1" * 32)
                self.assertFalse(verify_integral_set(index, root / "key"))
                self.assertTrue(verify_backup(media_a, self.KEY))

    def test_concurrent_rewrite_of_inventoried_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            asset = self._asset(root, 1, "logo", "a" * 32, b"first")
            inventory = _media_inventory(root)
            reader = _MediaArchiveReader(root, self.BACKUP_ID, inventory)
            reader.read(20)
            asset.write_bytes(b"changed-after-inventory")
            with self.assertRaisesRegex(RuntimeError, "cambió"):
                reader.read(1024)

    def test_remote_retention_keeps_integral_components_together(self):
        now = "2026-11-21T12:00:00Z"
        old = "2026-10-01T00:00:00Z"
        inventory = []
        for backup_id, modified in (("1" * 32, old), ("2" * 32, now)):
            for kind, suffix in (("set", ""), ("media", ""), ("postgres", ".dump")):
                base = f"j2m-{kind}-{backup_id}{suffix}.j2mbackup"
                for ending in ("", ".sha256", ".complete.json"):
                    inventory.append({"Path": base + ending, "ModTime": modified})
        plan = remote_retention_plan(inventory, 7)
        self.assertEqual(plan["protected"], f"j2m-set-{'2' * 32}.j2mbackup")
        self.assertEqual(len(plan["protected_files"]), 9)
        self.assertEqual(len(plan["delete"]), 9)

    def test_missing_or_wrong_operational_volume_fails_before_export(self):
        missing = MagicMock(returncode=1, stdout="")
        with patch("scripts.selfhost_backup.subprocess.run", return_value=missing):
            with self.assertRaisesRegex(RuntimeError, "no existe"):
                media_backup_from_docker("j2m-cutover-ready_j2m-media",
                    "j2m-interactive:" + "f" * 40, Path("unused"), Path("unused"),
                    self.BACKUP_ID)
        wrong = MagicMock(returncode=0, stdout=json.dumps([{
            "Name": "j2m-cutover-ready_j2m-media", "Driver": "local", "Scope": "local",
            "Mountpoint": "C:/safe", "Labels": {"com.docker.compose.project": "other",
                                                   "com.docker.compose.volume": "j2m-media"}}]))
        with patch("scripts.selfhost_backup.subprocess.run", return_value=wrong):
            with self.assertRaisesRegex(RuntimeError, "identidad.*Compose"):
                media_backup_from_docker("j2m-cutover-ready_j2m-media",
                    "j2m-interactive:" + "f" * 40, Path("unused"), Path("unused"),
                    self.BACKUP_ID)

    def test_integral_mode_never_falls_back_and_legacy_excludes_components(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "j2m-postgres-20261121T000000Z-abc.dump.j2mbackup.partial"
            with partial.open("wb") as output:
                encrypt_stream(io.BytesIO(b"legacy"), output, self.KEY)
            legacy = partial.with_suffix("")
            _publish_bundle(partial, legacy, "postgresql")
            component_partial = root / f"j2m-postgres-{self.BACKUP_ID}.dump.j2mbackup.partial"
            with component_partial.open("wb") as output:
                encrypt_stream(io.BytesIO(b"component"), output, self.KEY)
            component = component_partial.with_suffix("")
            _publish_bundle(component_partial, component, "postgresql", self.BACKUP_ID)
            with patch("scripts.selfhost_backup.load_dpapi_key", return_value=self.KEY):
                self.assertEqual(_complete_backups(root, self.KEY), [legacy])
                self.assertEqual(_select_backup(root, root / "key", "legacy"), legacy)
                with self.assertRaisesRegex(RuntimeError, "integral"):
                    _select_backup(root, root / "key", "integral")

    def test_remote_retention_protects_integral_and_legacy_independently(self):
        now = "2026-11-21T12:00:00Z"
        inventory = []
        for kind, suffix in (("set", ""), ("media", ""), ("postgres", ".dump")):
            base = f"j2m-{kind}-{'9' * 32}{suffix}.j2mbackup"
            inventory.extend({"Path": base + ending, "ModTime": now}
                             for ending in ("", ".sha256", ".complete.json"))
        legacy = "j2m-postgres-20261121T120000Z-abc.dump.j2mbackup"
        inventory.extend({"Path": legacy + ending, "ModTime": now}
                         for ending in ("", ".sha256", ".complete.json"))
        plan = remote_retention_plan(inventory, 7)
        self.assertCountEqual(plan["protected_backups"],
                              [f"j2m-set-{'9' * 32}.j2mbackup", legacy])
        self.assertEqual(plan["delete"], [])

    def test_restored_references_fail_for_missing_or_cross_tenant_media(self):
        valid = "businesses/1/logo/" + "a" * 32 + ".png"
        with patch("scripts.selfhost_backup._psql", return_value="1\tlogo\t" + valid):
            with self.assertRaisesRegex(RuntimeError, "omite"):
                _restored_media_references("pg", set())
        cross = "businesses/2/logo/" + "b" * 32 + ".png"
        with patch("scripts.selfhost_backup._psql", return_value="1\tlogo\t" + cross):
            with self.assertRaisesRegex(RuntimeError, "cruzada"):
                _restored_media_references("pg", {cross})

    def test_media_extractor_rejects_duplicate_paths_and_aggregate_limit(self):
        path = "businesses/1/logo/" + "a" * 32 + ".png"
        header = {"schema": 1, "backup_id": self.BACKUP_ID, "files": [
            {"path": path, "size": 0, "sha256": __import__("hashlib").sha256(b"").hexdigest()},
            {"path": path, "size": 0, "sha256": __import__("hashlib").sha256(b"").hexdigest()}]}
        raw = json.dumps(header).encode()
        from scripts.selfhost_backup import MEDIA_MAGIC
        import struct
        extractor = _MediaArchiveExtractor(None, self.BACKUP_ID)
        with self.assertRaisesRegex(ValueError, "duplicadas"):
            extractor.write(MEDIA_MAGIC + struct.pack(">I", len(raw)) + raw)
        second = "businesses/1/logo/" + "b" * 32 + ".png"
        aggregate = {"schema": 1, "backup_id": self.BACKUP_ID, "files": [
            {"path": path, "size": 1, "sha256": __import__("hashlib").sha256(b"a").hexdigest()},
            {"path": second, "size": 1, "sha256": __import__("hashlib").sha256(b"b").hexdigest()}]}
        raw = json.dumps(aggregate).encode()
        with patch("scripts.selfhost_backup.MEDIA_MAX_TOTAL_BYTES", 1):
            extractor = _MediaArchiveExtractor(None, self.BACKUP_ID)
            with self.assertRaisesRegex(ValueError, "acumulado"):
                extractor.write(MEDIA_MAGIC + struct.pack(">I", len(raw)) + raw + b"ab")


if __name__ == "__main__":
    unittest.main()
