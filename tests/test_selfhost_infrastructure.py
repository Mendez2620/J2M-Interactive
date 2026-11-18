import json
import hashlib
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts.portable_backup import _portable_text, decrypt_bytes, encrypt_bytes
from scripts.selfhost_backup import (
    CHUNK_SIZE,
    MAGIC,
    _manifest,
    _publish_bundle,
    _read_database_url,
    _sidecar,
    database_backup,
    decrypt_stream,
    encrypt_stream,
    exclusive_lock,
    load_dpapi_key,
    recover_key,
    remote_retention_plan,
    retain_local,
    restore_configuration,
    stage_offsite,
    verify_backup,
)


ROOT = Path(__file__).resolve().parent.parent


class SelfHostInfrastructureTestCase(unittest.TestCase):
    def _complete_backup(self, directory, name, cleartext, key):
        final = Path(directory) / name
        partial = final.with_name(final.name + ".partial")
        with partial.open("wb") as destination:
            encrypt_stream(io.BytesIO(cleartext), destination, key)
        _publish_bundle(partial, final, "test")
        return final
    def test_compose_separates_runtime_migration_and_tunnel(self):
        compose = (ROOT / "compose.selfhost.yml").read_text(encoding="utf-8")
        self.assertIn("web:", compose)
        self.assertIn("google-wallet-worker:", compose)
        self.assertIn("migrator:", compose)
        self.assertIn('profiles: ["migrate"]', compose)
        self.assertIn('restart: "no"', compose)
        self.assertIn("127.0.0.1:${J2M_LOCAL_PORT:-8000}:8000", compose)
        self.assertIn("J2M_PROCESS_TYPE: google-wallet-worker", compose)
        self.assertIn("cloudflare/cloudflared:2026.11.0@sha256:", compose)
        self.assertIn('profiles: ["tunnel"]', compose)
        self.assertIn("J2M_CLOUDFLARED_ENV_FILE:-.env.cloudflared", compose)
        self.assertNotIn("TUNNEL_TOKEN", (ROOT / ".env.selfhost.example").read_text(encoding="utf-8"))
        tunnel_env = (ROOT / ".env.cloudflared.example").read_text(encoding="utf-8")
        self.assertIn("TUNNEL_TOKEN=REQUIRED", tunnel_env)
        self.assertNotIn("DATABASE_URL", tunnel_env)
        self.assertNotIn("railway config apply", compose.lower())

    def test_rehearsal_is_isolated_and_wallet_disabled(self):
        overlay = (ROOT / "compose.selfhost.test.yml").read_text(encoding="utf-8")
        test_env = (ROOT / ".env.selfhost.test.example").read_text(encoding="utf-8")
        production_env = (ROOT / ".env.selfhost.example").read_text(encoding="utf-8")
        self.assertIn("postgres:16-alpine", overlay)
        self.assertIn("/var/lib/postgresql/data", overlay)
        self.assertNotIn("ports:", overlay)
        self.assertIn("internal: true", overlay)
        self.assertIn("@postgres-test:5432/j2m_test", test_env)
        for env_text in (test_env, production_env):
            self.assertIn("GOOGLE_WALLET_MODE=disabled", env_text)
            self.assertIn("APPLE_WALLET_MODE=disabled", env_text)
        self.assertNotIn("GOOGLE_SERVICE_ACCOUNT_JSON", test_env)

    def test_production_rehearsal_is_synthetic_tls_and_fail_closed(self):
        env_text = (ROOT / ".env.selfhost.production-test.example").read_text(encoding="utf-8")
        overlay = (ROOT / "compose.selfhost.production-test.yml").read_text(encoding="utf-8")
        self.assertIn("FLASK_ENV=production", env_text)
        self.assertIn("DATABASE_SSLMODE=require", env_text)
        self.assertIn("PUBLIC_BASE_URL=https://j2m.local.test", env_text)
        self.assertIn("CORS_ORIGINS=https://j2m.local.test", env_text)
        self.assertIn("WEBAUTHN_ORIGIN=https://j2m.local.test", env_text)
        self.assertIn("WEBAUTHN_RP_ID=j2m.local.test", env_text)
        self.assertIn("SESSION_COOKIE_SECURE=true", env_text)
        self.assertIn("GOOGLE_WALLET_MODE=disabled", env_text)
        self.assertIn("APPLE_WALLET_MODE=disabled", env_text)
        self.assertNotIn("REQUIRED", env_text)
        self.assertNotIn("supabase", env_text.lower())
        self.assertIn("ssl=on", overlay)
        self.assertIn("server.key", overlay)
        postgres_dockerfile = (ROOT / "Dockerfile.postgres-test").read_text(encoding="utf-8")
        self.assertIn("FROM postgres:16-alpine", postgres_dockerfile)
        self.assertIn("openssl", postgres_dockerfile)

    def test_loopback_overlay_keeps_database_internal_and_host_binding_local(self):
        base = (ROOT / "compose.selfhost.yml").read_text(encoding="utf-8")
        isolated = (ROOT / "compose.selfhost.test.yml").read_text(encoding="utf-8")
        loopback = (ROOT / "compose.selfhost.loopback-test.yml").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1:${J2M_LOCAL_PORT:-8000}:8000", base)
        self.assertIn("internal: true", isolated)
        self.assertIn("com.docker.network.bridge.host_binding_ipv4: 127.0.0.1", loopback)
        self.assertIn("j2m-loopback", loopback)
        self.assertNotIn("postgres-test:", loopback)

    def test_preflight_checks_profile_ports_and_both_wallet_modes(self):
        preflight = (ROOT / "scripts" / "selfhost_preflight.ps1").read_text(encoding="utf-8")
        self.assertIn('$pairs["J2M_LOCAL_PORT"]', preflight)
        self.assertIn('$pairs["CLOUDFLARED_METRICS_PORT"]', preflight)
        self.assertIn('Write-Check "google-disabled"', preflight)
        self.assertIn('Write-Check "apple-disabled"', preflight)
        self.assertIn("SkipExternalConnectivity", preflight)
        self.assertNotIn('$ports = @(80, 443, 2000, 5432, 8000, 8080)', preflight)

    def test_approved_release_script_rejects_unapproved_commits_and_migrations(self):
        deploy = (ROOT / "scripts" / "selfhost_deploy_approved.ps1").read_text(encoding="utf-8")
        self.assertIn("$ApprovedSha -ne $mainSha", deploy)
        self.assertIn("$headSha -ne $ApprovedSha", deploy)
        self.assertNotIn("db upgrade", deploy)
        self.assertNotIn("config apply", deploy)

    def test_ci_validates_compose_without_private_values(self):
        workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        self.assertIn("docker compose", workflow)
        self.assertIn("compose.selfhost.yml", workflow)
        self.assertIn("compose.selfhost.test.yml", workflow)
        self.assertIn("compose.selfhost.production-test.yml", workflow)
        self.assertIn("compose.selfhost.loopback-test.yml", workflow)
        self.assertIn("Dockerfile.postgres-test", workflow)
        self.assertIn("-p j2m-pr70-ci", workflow)
        self.assertIn("J2M_CLOUDFLARED_ENV_FILE: .env.cloudflared.example", workflow)
        package_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        self.assertIn("lockfileVersion", package_lock)

    def test_runbook_records_real_isolated_docker_validation(self):
        runbook = (ROOT / "docs" / "local-self-hosting.md").read_text(encoding="utf-8")
        self.assertIn("-p j2m-pr70-test", runbook)
        self.assertIn("PostgreSQL `16.16`", runbook)
        self.assertIn("9d0e1f2a3b4c", runbook)
        self.assertIn("/api/health", runbook)
        self.assertIn("FLASK_ENV=production", runbook)
        self.assertIn("127.0.0.1:18080", runbook)
        self.assertNotIn("Docker Desktop aún no está instalado", runbook)

    def test_private_artifacts_are_excluded_from_git_and_docker(self):
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
        for pattern in (".env.cloudflared", "*.dpapi", "*.j2mbackup", "selfhost-backups"):
            self.assertIn(pattern, gitignore)
            self.assertIn(pattern, dockerignore)

    def test_portable_backup_encryption_round_trip_and_authentication(self):
        cleartext = b'{"DATABASE_URL":"not-a-real-secret"}'
        password = b"portable-test-password-123"
        payload = encrypt_bytes(cleartext, password)
        self.assertNotIn(cleartext, payload)
        self.assertEqual(decrypt_bytes(payload, password), cleartext)
        with self.assertRaises(Exception):
            decrypt_bytes(payload, b"wrong-password-value")
        self.assertEqual(_portable_text("texto".encode("utf-16-le")), b"texto")

    def test_stream_backup_encrypts_before_disk_and_detects_tampering(self):
        cleartext = (b"synthetic-postgresql-dump\x00" * 100_000)
        key = bytes(range(32))
        encrypted = io.BytesIO()
        digest = encrypt_stream(io.BytesIO(cleartext), encrypted, key)
        payload = encrypted.getvalue()
        self.assertNotIn(cleartext[:100], payload)
        restored = io.BytesIO()
        frames, total = decrypt_stream(io.BytesIO(payload), restored, key)
        self.assertEqual(restored.getvalue(), cleartext)
        self.assertGreater(frames, 1)
        self.assertEqual(total, len(cleartext))
        altered = bytearray(payload)
        altered[-8] ^= 1
        with self.assertRaises(Exception):
            decrypt_stream(io.BytesIO(altered), io.BytesIO(), key)
        self.assertEqual(len(digest), 64)

    def test_stream_rejects_truncation_removed_reordered_and_invalid_frames(self):
        key = bytes(range(32))
        encrypted = io.BytesIO()
        encrypt_stream(io.BytesIO(b"A" * (CHUNK_SIZE * 2 + 9)), encrypted, key)
        payload = encrypted.getvalue()
        header_size = len(MAGIC) + 8 + 4
        records = []
        offset = header_size
        while offset < len(payload):
            length = int.from_bytes(payload[offset + 1 : offset + 5], "big")
            records.append(payload[offset : offset + 5 + length])
            offset += 5 + length
        variants = [
            payload[:-1],
            payload[:header_size] + records[0] + records[-1],
            payload[:header_size] + records[1] + records[0] + b"".join(records[2:]),
            payload[: header_size + 1] + (0xFFFFFFFF).to_bytes(4, "big") + payload[header_size + 5 :],
        ]
        for variant in variants:
            with self.subTest(size=len(variant)):
                with self.assertRaises(Exception):
                    decrypt_stream(io.BytesIO(variant), io.BytesIO(), key)

    def test_recomputed_external_hash_cannot_validate_truncated_authenticated_stream(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = bytes(range(32))
            backup = self._complete_backup(root, "backup.j2mbackup", b"payload" * 100, key)
            backup.write_bytes(backup.read_bytes()[:-1])
            digest = hashlib.sha256(backup.read_bytes()).hexdigest()
            _sidecar(backup).write_text(f"{digest}  {backup.name}\n", encoding="ascii")
            manifest = json.loads(_manifest(backup).read_text(encoding="utf-8"))
            manifest["sha256"] = digest
            manifest["size"] = backup.stat().st_size
            _manifest(backup).write_text(json.dumps(manifest), encoding="utf-8")
            self.assertFalse(verify_backup(backup, key))

    def test_dpapi_binary_key_is_not_text_normalized(self):
        binary_key = bytes([255, 0, 254, 1]) + bytes(range(28))
        with tempfile.TemporaryDirectory() as temporary:
            wrapped = Path(temporary) / "key.dpapi"
            wrapped.write_bytes(b"wrapped")
            with patch("scripts.selfhost_backup._unprotect_dpapi", return_value=binary_key):
                self.assertEqual(load_dpapi_key(wrapped), binary_key)

    def test_portable_recovery_creates_new_dpapi_binding_and_never_overwrites(self):
        key = bytes([17, 0, 255]) + bytes(range(29))
        password = b"portable-recovery-password"
        with tempfile.TemporaryDirectory() as temporary:
            recovery = Path(temporary) / "recovery.j2mbackup"
            target = Path(temporary) / "new-profile.dpapi"
            recovery.write_bytes(encrypt_bytes(key, password))
            with patch("scripts.selfhost_backup._password", return_value=password), patch(
                "scripts.selfhost_backup._protect_dpapi", side_effect=lambda value: b"wrapped:" + value
            ), patch(
                "scripts.selfhost_backup._unprotect_dpapi", side_effect=lambda value: value.removeprefix(b"wrapped:")
            ):
                recover_key(recovery, target)
                self.assertEqual(load_dpapi_key(target), key)
                with self.assertRaises(ValueError):
                    recover_key(recovery, target)

    @unittest.skipUnless(os.name == "nt", "DPAPI solo está disponible en Windows")
    def test_portable_recovery_rebinds_real_dpapi_for_current_windows_profile(self):
        key = bytes([201, 0, 3]) + bytes(range(29))
        password = b"synthetic-portable-password"
        with tempfile.TemporaryDirectory() as temporary:
            recovery = Path(temporary) / "recovery.j2mbackup"
            target = Path(temporary) / "profile.dpapi"
            recovery.write_bytes(encrypt_bytes(key, password))
            with patch("scripts.selfhost_backup._password", return_value=password):
                recover_key(recovery, target)
            self.assertEqual(load_dpapi_key(target), key)

    def test_sha256_and_local_retention_keep_latest_48(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            key = bytes(range(32))
            for index in range(50):
                backup = self._complete_backup(directory, f"{index:02d}.j2mbackup", str(index).encode(), key)
                os.utime(backup, (index + 1, index + 1))
                os.utime(_sidecar(backup), (index + 1, index + 1))
                os.utime(_manifest(backup), (index + 1, index + 1))
            newest = directory / "49.j2mbackup"
            self.assertTrue(verify_backup(newest, key))
            self.assertEqual(retain_local(directory, key, 48), 2)
            self.assertEqual(len(list(directory.glob("*.j2mbackup"))), 48)
            self.assertEqual(retain_local(directory, key, 0), 47)
            self.assertTrue(verify_backup(newest, key))

    def test_local_retention_ignores_manifest_with_recomputed_hash_but_invalid_aead(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = bytes(range(32))
            valid = self._complete_backup(root, "valid.j2mbackup", b"valid", key)
            invalid = self._complete_backup(root, "invalid.j2mbackup", b"invalid", key)
            invalid.write_bytes(invalid.read_bytes()[:-1])
            digest = hashlib.sha256(invalid.read_bytes()).hexdigest()
            _sidecar(invalid).write_text(f"{digest}  {invalid.name}\n", encoding="ascii")
            manifest = json.loads(_manifest(invalid).read_text(encoding="utf-8"))
            manifest.update(sha256=digest, size=invalid.stat().st_size)
            _manifest(invalid).write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(retain_local(root, key, 1), 0)
            self.assertTrue(valid.exists())
            self.assertTrue(invalid.exists())

    def test_offsite_staging_uses_only_latest_complete_verified_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, external = root / "hourly", root / "external-download"
            source.mkdir()
            key = bytes(range(32))
            first = self._complete_backup(source, "01.j2mbackup", b"old", key)
            second = self._complete_backup(source, "02.j2mbackup", b"new", key)
            os.utime(first, (1, 1))
            os.utime(second, (2, 2))
            incomplete = source / "03.j2mbackup"
            incomplete.write_bytes(b"not-complete")
            with patch("scripts.selfhost_backup.load_dpapi_key", return_value=key):
                selected = stage_offsite(source, external, root / "key.dpapi")
            self.assertEqual(selected.name, second.name)
            self.assertTrue(verify_backup(selected, key))
            self.assertEqual(len(list(external.glob("*.j2mbackup"))), 1)
            self.assertTrue(_sidecar(selected).exists())
            self.assertTrue(_manifest(selected).exists())

    def test_private_configuration_restores_to_new_machine_directory_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key = bytes(range(32))
            archive = io.BytesIO()
            import zipfile
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(".env.selfhost", "SECRET_KEY=synthetic\n")
                bundle.writestr(".env.cloudflared", "TUNNEL_TOKEN=synthetic\n")
            backup = self._complete_backup(root, "configuration.zip.j2mbackup", archive.getvalue(), key)
            destination = root / "recovered-on-new-machine"
            with patch("scripts.selfhost_backup.load_dpapi_key", return_value=key):
                restored = restore_configuration(backup, destination, root / "profile.dpapi")
                self.assertEqual(restored, [".env.cloudflared", ".env.selfhost"])
                self.assertEqual((destination / ".env.selfhost").read_text(), "SECRET_KEY=synthetic\n")
                with self.assertRaises(ValueError):
                    restore_configuration(backup, destination, root / "profile.dpapi")

    @unittest.skipUnless(shutil.which("rclone"), "rclone no está instalado")
    def test_rclone_exact_retention_protects_each_category_independently(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, remote = root / "source", root / "remote"
            key = bytes(range(32))
            now = datetime(2026, 11, 18, tzinfo=timezone.utc)
            scenarios = {
                "four-hour": [("j2m-postgres-four-old", 10, True), ("j2m-postgres-four-last", 8, True)],
                "daily": [("j2m-postgres-daily-old", 45, True), ("j2m-postgres-daily-last", 35, True)],
                "configuration": [("j2m-configuration-last", 90, True), ("j2m-configuration-incomplete", 60, False)],
            }
            expected_protected = {
                "four-hour": "j2m-postgres-four-last.j2mbackup",
                "daily": "j2m-postgres-daily-last.j2mbackup",
                "configuration": "j2m-configuration-last.j2mbackup",
            }
            for category, entries in scenarios.items():
                local_category = source / category
                local_category.mkdir(parents=True)
                for name, age_days, complete in entries:
                    backup = self._complete_backup(local_category, f"{name}.j2mbackup", name.encode(), key)
                    if not complete:
                        _manifest(backup).unlink()
                    stamp = (now - timedelta(days=age_days)).timestamp()
                    for item in local_category.glob(f"{backup.name}*"):
                        os.utime(item, (stamp, stamp))
                subprocess.run(["rclone", "copy", str(local_category), str(remote / category)], check=True)
                inventory = json.loads(subprocess.run(
                    ["rclone", "lsjson", str(remote / category), "--files-only", "--no-mimetype"],
                    check=True, capture_output=True, text=True,
                ).stdout)
                days = 7 if category == "four-hour" else 30
                plan = remote_retention_plan(inventory, days, now)
                self.assertEqual(plan["protected"], expected_protected[category])
                for relative in plan["delete"]:
                    subprocess.run(["rclone", "deletefile", str(remote / category / relative)], check=True)
                remaining = subprocess.run(
                    ["rclone", "lsf", str(remote / category), "--files-only"],
                    check=True, capture_output=True, text=True,
                ).stdout
                self.assertIn(plan["protected"], remaining)
                if category != "configuration":
                    self.assertNotIn(entries[0][0] + ".j2mbackup", remaining)
                else:
                    self.assertNotIn("j2m-configuration-incomplete.j2mbackup", remaining)

    def test_operation_lock_prevents_concurrent_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "operation.lock"
            entered = threading.Event()
            release = threading.Event()
            def holder():
                with exclusive_lock(lock):
                    entered.set()
                    release.wait(2)
            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(entered.wait(1))
            with self.assertRaises(RuntimeError):
                with exclusive_lock(lock):
                    pass
            release.set()
            thread.join()

    def test_pg_dump_url_forces_hostname_verification_and_is_synthetic_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = Path(temporary) / "env"
            env.write_text("SECRET_KEY=not-forwarded\nDATABASE_URL=postgresql://u:p@db.example/x?sslmode=require\n", encoding="utf-8")
            value = _read_database_url(env)
            self.assertIn("sslmode=verify-full", value)
            self.assertIn("sslrootcert=system", value)
            self.assertNotIn("SECRET_KEY", value)
            env.write_text("DATABASE_URL=postgresql://u:p@j2m-backup-source/x?sslmode=disable\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                _read_database_url(env, allow_insecure_synthetic=True)
            with patch.dict(os.environ, {"J2M_SYNTHETIC_BACKUP_TEST": "1"}):
                self.assertIn("sslmode=disable", _read_database_url(env, True))

    def test_database_container_receives_only_database_url_and_publishes_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = root / "app.env"
            env.write_text(
                "SECRET_KEY=must-not-enter-container\n"
                "DATABASE_URL=postgresql://u:p@db.example/j2m?sslmode=require\n",
                encoding="utf-8",
            )
            captured = {}
            key = bytes(range(32))
            def fake_dump(command, child_env, output, received_key, timeout):
                captured["command"] = command
                captured["database_url"] = child_env["DATABASE_URL"]
                with output.open("wb") as stream:
                    encrypt_stream(io.BytesIO(b"synthetic-dump"), stream, received_key)
            with patch("scripts.selfhost_backup.load_dpapi_key", return_value=key), patch(
                "scripts.selfhost_backup._run_streaming_dump", side_effect=fake_dump
            ):
                backup = database_backup(env, root / "hourly", root / "key.dpapi", retries=1)
            self.assertNotIn("--env-file", captured["command"])
            self.assertEqual(captured["command"].count("-e"), 1)
            self.assertIn("verify-full", captured["database_url"])
            self.assertTrue(verify_backup(backup, key))
            self.assertTrue(_manifest(backup).exists())

    def test_scheduler_is_fail_closed_until_cloud_and_key_are_authorized(self):
        installer = (ROOT / "scripts" / "install_selfhost_backup_tasks.ps1").read_text(encoding="utf-8")
        runner = (ROOT / "scripts" / "run_selfhost_backup.ps1").read_text(encoding="utf-8")
        self.assertIn("if (-not $Enable)", installer)
        self.assertIn("CloudRemote es obligatorio", installer)
        self.assertIn("MultipleInstances IgnoreNew", installer)
        self.assertIn("-LogonType Interactive", installer)
        self.assertNotIn("-LogonType S4U", installer)
        self.assertIn("database --env-file", runner)
        cloud = (ROOT / "scripts" / "run_selfhost_cloud_copy.ps1").read_text(encoding="utf-8")
        self.assertIn("rclone copy", cloud)
        self.assertIn("remote cryptographic verification failed", cloud)
        self.assertNotIn("rclone sync", cloud.lower())
        self.assertNotIn("--exclude", cloud)
        self.assertNotIn("rclone delete ", cloud)
        self.assertIn("rclone deletefile", cloud)
        self.assertIn('"$Remote/four-hour"', cloud)
        self.assertIn('"$Remote/daily"', cloud)
        self.assertIn('Invoke-ExactRetention "four-hour" 7', cloud)
        self.assertIn('Invoke-ExactRetention "daily" 30', cloud)
        self.assertIn('Invoke-ExactRetention "configuration" 30', cloud)
        restore = (ROOT / "scripts" / "run_selfhost_restore_test.ps1").read_text(encoding="utf-8")
        self.assertIn("restore-isolated", restore)
        backup_script = (ROOT / "scripts" / "selfhost_backup.py").read_text(encoding="utf-8")
        self.assertIn('"--network", "none"', backup_script)
        self.assertIn('"postgres:17-alpine"', backup_script)
        self.assertIn("/etc/ssl/certs/ca-certificates.crt", backup_script)
        self.assertIn("backup.failed", runner)
        self.assertIn("cloud.failed", cloud)
        self.assertIn("restore.failed", restore)

    @unittest.skipUnless(os.name == "nt", "ACL y Task Scheduler solo se validan en Windows")
    def test_windows_acl_and_scheduler_plan_execute_without_registering_tasks(self):
        (ROOT / "selfhost-backups").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "selfhost-backups") as temporary:
            secret = Path(temporary) / "secret.txt"
            secret.write_text("synthetic", encoding="utf-8")
            acl_result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / "scripts" / "protect_selfhost_secrets.ps1"), "-Path", str(secret)],
                capture_output=True, text=True,
            )
            self.assertEqual(acl_result.returncode, 0, acl_result.stdout + acl_result.stderr)
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(ROOT / "scripts" / "install_selfhost_backup_tasks.ps1")],
                check=True, capture_output=True, text=True,
            )
            self.assertIn("no se registraron tareas", result.stdout)


if __name__ == "__main__":
    unittest.main()
