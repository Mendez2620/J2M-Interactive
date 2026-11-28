import json
import hashlib
import http.server
import io
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
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
        kind = "configuration" if "configuration" in name else "postgresql"
        _publish_bundle(partial, final, kind)
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
        self.assertIn("set_selfhost_image_pin.ps1", deploy)
        self.assertNotIn("$env:J2M_IMAGE", deploy)
        self.assertIn("$previousSha", deploy)
        self.assertIn("la imagen y el pin anteriores fueron restaurados", deploy)
        self.assertIn("invoke_selfhost_compose.ps1", deploy)
        self.assertIn("--wait-timeout 120", deploy)
        self.assertIn("$ApprovedSha -eq $previousSha", deploy)
        self.assertIn("Test-J2MLocalHttps", deploy)
        self.assertIn("--expected-revision", deploy)
        gate = 'Confirm-J2MLocalProxy $LocalHttpsOrigin $previousImage -VerifyOnly'
        self.assertLess(deploy.index(gate), deploy.index("docker build"))
        self.assertLess(deploy.index(gate), deploy.index("set_selfhost_image_pin.ps1"))
        self.assertIn("--verify-only", deploy)
        self.assertIn('Headers["X-J2M-Revision"]', deploy)
        self.assertIn("selfhost_proxy_recovery.py", deploy)
        self.assertIn("Confirm-J2MLocalProxy", deploy)
        self.assertIn('"/api/health", "/api/ready"', deploy)
        self.assertLess(deploy.index("try {", deploy.index('$services = @')), deploy.index("set_selfhost_image_pin.ps1", deploy.index('$services = @')))
        self.assertNotIn("db upgrade", deploy)
        self.assertNotIn("config apply", deploy)

    def test_selfhost_image_pin_is_persistent_exact_and_fail_closed(self):
        compose = (ROOT / "compose.selfhost.yml").read_text(encoding="utf-8")
        template = (ROOT / ".env.selfhost.example").read_text(encoding="utf-8")
        preflight = (ROOT / "scripts" / "selfhost_preflight.ps1").read_text(encoding="utf-8")
        pin = (ROOT / "scripts" / "set_selfhost_image_pin.ps1").read_text(encoding="utf-8")
        self.assertIn("${J2M_IMAGE:?", compose)
        self.assertNotIn("J2M_IMAGE:-", compose)
        self.assertIn("J2M_IMAGE=REQUIRED_APPROVED_IMAGE_TAG", template)
        self.assertIn('^j2m-interactive:[0-9a-f]{40}$', preflight)
        self.assertIn('Write-Check "image-pin-local"', preflight)
        self.assertIn("git merge-base --is-ancestor", pin)
        self.assertIn("docker image inspect", pin)
        self.assertIn("protect_selfhost_secrets.ps1", pin)
        self.assertIn("$pins.Count -ne 1", pin)
        self.assertLess(pin.index("protect_selfhost_secrets.ps1"), pin.index("WriteAllLines"))

        wrapper = (ROOT / "scripts" / "invoke_selfhost_compose.ps1").read_text(encoding="utf-8")
        self.assertIn('$projectName = "j2m-cutover-ready"', wrapper)
        self.assertIn('SetEnvironmentVariable("J2M_IMAGE", $pairs["J2M_IMAGE"]', wrapper)
        self.assertIn('SetEnvironmentVariable("J2M_ENV_FILE", $envPath.Path', wrapper)
        self.assertIn("$previousImage", wrapper)
        self.assertIn("$previousEnvFile", wrapper)
        self.assertIn("google-wallet-worker", wrapper)
        self.assertIn("fuera del proyecto autorizado", wrapper)
        self.assertIn("{{json .Config.Labels}}", wrapper)
        self.assertIn("'com.docker.compose.project'", wrapper)

    @unittest.skipUnless(os.name == "nt", "PowerShell ACL integration only runs on Windows")
    def test_selfhost_image_pin_replaces_duplicates_and_preserves_acl(self):
        approved = "1" * 40
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / ".env.selfhost"
            env_file.write_text(
                "SECRET_KEY=opaque-test-value\n"
                "J2M_IMAGE=j2m-interactive:" + ("0" * 40) + "\n"
                "J2M_IMAGE=j2m-interactive:duplicate\n",
                encoding="utf-8",
            )
            protect = ROOT / "scripts" / "protect_selfhost_secrets.ps1"
            pin = ROOT / "scripts" / "set_selfhost_image_pin.ps1"
            command = (
                "function global:git { $global:LASTEXITCODE = 0 }; "
                "function global:docker { $global:LASTEXITCODE = 0 }; "
                f"& '{protect}' -Path '{env_file}'; "
                f"& '{pin}' -ApprovedSha {approved} -EnvFile '{env_file}'; "
                f"$info = Get-Item -LiteralPath '{env_file}'; "
                "$aclType = [Type]::GetType('System.IO.FileSystemAclExtensions, System.IO.FileSystem.AccessControl', $false); "
                f"$acl = if ($null -ne $aclType) {{ $aclType::GetAccessControl($info) }} else {{ [System.IO.File]::GetAccessControl('{env_file}') }}; "
                "if (-not $acl.AreAccessRulesProtected) { exit 9 }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            contents = env_file.read_text(encoding="utf-8")
            self.assertIn("SECRET_KEY=opaque-test-value", contents)
            self.assertEqual(contents.count("J2M_IMAGE="), 1)
            self.assertIn(f"J2M_IMAGE=j2m-interactive:{approved}", contents)

    @unittest.skipUnless(os.name == "nt", "PowerShell compose integration only runs on Windows")
    def test_compose_wrapper_ignores_inherited_image_and_env_file(self):
        approved = "2" * 40
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env.selfhost"
            env_file.write_text(f"J2M_IMAGE=j2m-interactive:{approved}\n", encoding="utf-8")
            wrapper = ROOT / "scripts" / "invoke_selfhost_compose.ps1"
            command = (
                "$env:J2M_IMAGE='j2m-interactive:inherited'; $env:J2M_ENV_FILE='inherited.env'; "
                "function global:docker { "
                "if ($args[0] -eq 'ps') { $global:LASTEXITCODE=0; return }; "
                "Write-Output ('CALL=' + ($args -join ' ')); "
                "Write-Output ('CONTROLLED_IMAGE=' + $env:J2M_IMAGE); "
                "Write-Output ('CONTROLLED_ENV=' + $env:J2M_ENV_FILE); $global:LASTEXITCODE=0 }; "
                f"& '{wrapper}' -EnvFile '{env_file}' config --quiet; "
                "Write-Output ('RESTORED_IMAGE=' + $env:J2M_IMAGE); "
                "Write-Output ('RESTORED_ENV=' + $env:J2M_ENV_FILE)"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=30, cwd=ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(f"CONTROLLED_IMAGE=j2m-interactive:{approved}", result.stdout)
            self.assertIn(f"CONTROLLED_ENV={env_file}", result.stdout)
            self.assertIn("--project-name j2m-cutover-ready", result.stdout)
            self.assertIn("RESTORED_IMAGE=j2m-interactive:inherited", result.stdout)
            self.assertIn("RESTORED_ENV=inherited.env", result.stdout)

    @unittest.skipUnless(os.name == "nt", "PowerShell compose integration only runs on Windows")
    def test_compose_wrapper_rejects_foreign_worker(self):
        approved = "3" * 40
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env.selfhost"
            env_file.write_text(f"J2M_IMAGE=j2m-interactive:{approved}\n", encoding="utf-8")
            wrapper = ROOT / "scripts" / "invoke_selfhost_compose.ps1"
            command = (
                "function global:docker { "
                "if ($args[0] -eq 'ps') { Write-Output 'foreign-worker'; $global:LASTEXITCODE=0; return }; "
                "if ($args[0] -eq 'inspect') { Write-Output '{\"com.docker.compose.project\":\"other-project\"}'; $global:LASTEXITCODE=0; return }; "
                "throw 'compose must not run' }; "
                f"& '{wrapper}' -EnvFile '{env_file}' up -d google-wallet-worker"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=30, cwd=ROOT,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("fuera del proyecto autorizado", result.stdout + result.stderr)
            self.assertNotIn("compose must not run", result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "nt", "PowerShell deploy integration only runs on Windows")
    def test_failed_deploy_restores_previous_pin_and_image(self):
        previous = "5" * 40
        requested = "4" * 40
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env.selfhost"
            env_file.write_text(f"J2M_IMAGE=j2m-interactive:{previous}\n", encoding="utf-8")
            protect = ROOT / "scripts" / "protect_selfhost_secrets.ps1"
            deploy = ROOT / "scripts" / "selfhost_deploy_approved.ps1"
            command = (
                f"& '{protect}' -Path '{env_file}'; "
                f"$newSha='{requested}'; $global:upCalls=0; $global:httpsCalls=0; $global:proxyCalls=0; "
                "function global:python { $global:proxyCalls++; $global:LASTEXITCODE=0 }; "
                "function global:git { "
                "if ($args[0] -eq 'rev-parse') { Write-Output $newSha }; $global:LASTEXITCODE=0 }; "
                "function global:docker { $joined=$args -join ' '; "
                "if ($args[0] -eq 'ps') { $global:LASTEXITCODE=0; return }; "
                "if ($args[0] -eq 'compose' -and $joined -match ' up ') { "
                "$global:upCalls++; if ($env:J2M_IMAGE -eq ('j2m-interactive:' + $newSha)) { $global:LASTEXITCODE=1 } "
                "else { $global:LASTEXITCODE=0 }; return }; $global:LASTEXITCODE=0 }; "
                "function global:Invoke-WebRequest { $global:httpsCalls++; [pscustomobject]@{StatusCode=200; Headers=@{'X-J2M-Revision'=('5' * 40)}} }; "
                f"try {{ & '{deploy}' -ApprovedSha $newSha -EnvFile '{env_file}' }} "
                "catch { Write-Output ('DEPLOY_ERROR=' + $_.Exception.Message); "
                "Write-Output ('UP_CALLS=' + $global:upCalls); Write-Output ('HTTPS_CALLS=' + $global:httpsCalls); "
                "Write-Output ('PROXY_CALLS=' + $global:proxyCalls); exit 7 }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=30, cwd=ROOT,
            )
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertIn("la imagen y el pin anteriores fueron restaurados", result.stdout)
            self.assertIn("UP_CALLS=2", result.stdout)
            self.assertIn("HTTPS_CALLS=4", result.stdout)
            self.assertIn("PROXY_CALLS=2", result.stdout)
            contents = env_file.read_text(encoding="utf-8")
            self.assertEqual(contents.count("J2M_IMAGE="), 1)
            self.assertIn(f"J2M_IMAGE=j2m-interactive:{previous}", contents)

    @unittest.skipUnless(os.name == "nt", "PowerShell deploy integration only runs on Windows")
    def test_deploy_rejects_sha_equal_to_rollback_image_before_build(self):
        approved = "6" * 40
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env.selfhost"
            env_file.write_text(f"J2M_IMAGE=j2m-interactive:{approved}\n", encoding="utf-8")
            protect = ROOT / "scripts" / "protect_selfhost_secrets.ps1"
            deploy = ROOT / "scripts" / "selfhost_deploy_approved.ps1"
            command = (
                f"& '{protect}' -Path '{env_file}'; $sha='{approved}'; $global:buildCalls=0; "
                "function global:git { if ($args[0] -eq 'rev-parse') { Write-Output $sha }; $global:LASTEXITCODE=0 }; "
                "function global:docker { if ($args[0] -eq 'build') { $global:buildCalls++ }; $global:LASTEXITCODE=0 }; "
                f"try {{ & '{deploy}' -ApprovedSha $sha -EnvFile '{env_file}' }} catch {{ "
                "Write-Output ('DEPLOY_ERROR=' + $_.Exception.Message); Write-Output ('BUILD_CALLS=' + $global:buildCalls); exit 6 }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=30, cwd=ROOT,
            )
            self.assertEqual(result.returncode, 6, result.stdout + result.stderr)
            self.assertIn("se rechaza reconstruir", result.stdout)
            self.assertIn("BUILD_CALLS=0", result.stdout)
            self.assertIn(f"J2M_IMAGE=j2m-interactive:{approved}", env_file.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "PowerShell deploy integration only runs on Windows")
    def test_local_https_failure_triggers_verified_rollback(self):
        previous = "7" * 40
        requested = "8" * 40
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env.selfhost"
            env_file.write_text(f"J2M_IMAGE=j2m-interactive:{previous}\n", encoding="utf-8")
            protect = ROOT / "scripts" / "protect_selfhost_secrets.ps1"
            deploy = ROOT / "scripts" / "selfhost_deploy_approved.ps1"
            command = (
                f"& '{protect}' -Path '{env_file}'; $newSha='{requested}'; "
                "$global:upCalls=0; $global:httpsCalls=0; $global:proxyCalls=0; "
                "function global:python { $global:proxyCalls++; $global:LASTEXITCODE=0 }; "
                "function global:git { if ($args[0] -eq 'rev-parse') { Write-Output $newSha }; $global:LASTEXITCODE=0 }; "
                "function global:docker { $joined=$args -join ' '; if ($args[0] -eq 'ps') { $global:LASTEXITCODE=0; return }; "
                "if ($args[0] -eq 'compose' -and $joined -match ' up ') { $global:upCalls++ }; $global:LASTEXITCODE=0 }; "
                "function global:Invoke-WebRequest { $global:httpsCalls++; if ($global:httpsCalls -eq 3) { throw 'synthetic HTTPS failure' }; "
                "[pscustomobject]@{StatusCode=200; Headers=@{'X-J2M-Revision'=('7' * 40)}} }; "
                f"try {{ & '{deploy}' -ApprovedSha $newSha -EnvFile '{env_file}' }} catch {{ "
                "Write-Output ('DEPLOY_ERROR=' + $_.Exception.Message); Write-Output ('UP_CALLS=' + $global:upCalls); "
                "Write-Output ('HTTPS_CALLS=' + $global:httpsCalls); Write-Output ('PROXY_CALLS=' + $global:proxyCalls); exit 7 }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=30, cwd=ROOT,
            )
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertIn("la imagen y el pin anteriores fueron restaurados", result.stdout)
            self.assertIn("UP_CALLS=2", result.stdout)
            self.assertIn("HTTPS_CALLS=5", result.stdout)
            self.assertIn("PROXY_CALLS=3", result.stdout)
            self.assertIn(f"J2M_IMAGE=j2m-interactive:{previous}", env_file.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "PowerShell deploy integration only runs on Windows")
    def test_unverified_rollback_stops_before_deployment_mutations(self):
        previous, requested = "9" * 40, "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env.selfhost"
            original = f"J2M_IMAGE=j2m-interactive:{previous}\n"
            env_file.write_text(original, encoding="utf-8")
            deploy = ROOT / "scripts" / "selfhost_deploy_approved.ps1"
            command = (
                f"$newSha='{requested}'; $global:mutations=0; "
                "function global:git { if ($args[0] -eq 'rev-parse') { Write-Output $newSha }; $global:LASTEXITCODE=0 }; "
                "function global:docker { if ($args -contains 'up' -or $args -contains 'build') { $global:mutations++ }; "
                "$global:LASTEXITCODE=0 }; "
                "function global:python { if ($args -notcontains '--verify-only') { $global:mutations++ }; $global:LASTEXITCODE=1 }; "
                f"try {{ & '{deploy}' -ApprovedSha $newSha -EnvFile '{env_file}' }} catch {{ "
                "Write-Output ('MUTATIONS=' + $global:mutations); "
                "Write-Output $_.Exception.Message; exit 7 }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=30, cwd=ROOT,
            )
            self.assertEqual(result.returncode, 7, result.stdout + result.stderr)
            self.assertIn("MUTATIONS=0", result.stdout)
            self.assertIn("No se pudo confirmar", result.stdout)
            self.assertEqual(env_file.read_text(encoding="utf-8"), original)

    @unittest.skipUnless(os.name == "nt", "PowerShell deploy integration only runs on Windows")
    def test_successful_deploy_requires_both_local_https_checks(self):
        previous = "9" * 40
        requested = "a" * 40
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env.selfhost"
            env_file.write_text(f"J2M_IMAGE=j2m-interactive:{previous}\n", encoding="utf-8")
            protect = ROOT / "scripts" / "protect_selfhost_secrets.ps1"
            deploy = ROOT / "scripts" / "selfhost_deploy_approved.ps1"
            command = (
                f"& '{protect}' -Path '{env_file}'; $newSha='{requested}'; $global:httpsCalls=0; $global:proxyCalls=0; "
                "function global:python { $global:proxyCalls++; $global:LASTEXITCODE=0 }; "
                "function global:git { if ($args[0] -eq 'rev-parse') { Write-Output $newSha }; $global:LASTEXITCODE=0 }; "
                "function global:docker { if ($args[0] -eq 'ps') { $global:LASTEXITCODE=0; return }; $global:LASTEXITCODE=0 }; "
                "function global:Invoke-WebRequest { $global:httpsCalls++; [pscustomobject]@{StatusCode=200; Headers=@{'X-J2M-Revision'=$(if ($global:httpsCalls -le 2) { '9' * 40 } else { 'a' * 40 })}} }; "
                f"& '{deploy}' -ApprovedSha $newSha -EnvFile '{env_file}'; Write-Output ('HTTPS_CALLS=' + $global:httpsCalls); "
                "Write-Output ('PROXY_CALLS=' + $global:proxyCalls)"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=30, cwd=ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("HTTPS_CALLS=4", result.stdout)
            self.assertIn("PROXY_CALLS=2", result.stdout)
            self.assertIn(f"J2M_IMAGE=j2m-interactive:{requested}", env_file.read_text(encoding="utf-8"))

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
            explicit = _read_database_url(env, sslrootcert="/run/j2m/database-root-ca.crt")
            self.assertIn("sslrootcert=%2Frun%2Fj2m%2Fdatabase-root-ca.crt", explicit)
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
                "DATABASE_URL=postgresql://u:p@db.example/j2m?sslmode=require\n"
                f"DATABASE_SSL_ROOT_CERT={root / 'supabase-ca.crt'}\n",
                encoding="utf-8",
            )
            (root / "supabase-ca.crt").write_text("synthetic-ca", encoding="ascii")
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
            self.assertIn("--mount", captured["command"])
            self.assertTrue(any("database-root-ca.crt" in item for item in captured["command"]))
            self.assertTrue(verify_backup(backup, key))
            self.assertTrue(_manifest(backup).exists())

    def test_scheduler_is_fail_closed_until_cloud_and_key_are_authorized(self):
        installer = (ROOT / "scripts" / "install_selfhost_backup_tasks.ps1").read_text(encoding="utf-8")
        runner = (ROOT / "scripts" / "run_selfhost_backup.ps1").read_text(encoding="utf-8")
        self.assertIn("if (-not $Enable)", installer)
        self.assertIn("CloudRemote es obligatorio", installer)
        self.assertIn("MultipleInstances IgnoreNew", installer)
        self.assertIn("-AllowStartIfOnBatteries", installer)
        self.assertIn("-DontStopIfGoingOnBatteries", installer)
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
        self.assertIn("pg_restore --list", backup_script)
        self.assertIn("EXTENSION - supabase_vault|", backup_script)
        self.assertIn('"--exclude-schema=vault"', backup_script)
        self.assertIn("backup.failed", runner)
        self.assertIn("cloud.failed", cloud)
        self.assertIn("restore.failed", restore)
        monitor = (ROOT / "scripts" / "healthchecks_monitor.py").read_text(encoding="utf-8")
        self.assertIn("J2M_HEALTHCHECKS_FILE", monitor)
        self.assertIn('STATES = {"start", "success", "fail"}', monitor)
        self.assertIn("hc-ping.com", monitor)
        self.assertIn("timeout=10", monitor)
        self.assertNotIn("print(", monitor)
        runtime = (ROOT / "scripts" / "selfhost_task_runtime.ps1").read_text(encoding="utf-8")
        self.assertIn("Send-J2MHealthcheck", runtime)
        self.assertIn("Enter-J2MSelfHostTaskGuard", runtime)
        self.assertIn("Wait-J2MDockerReady", runtime)
        self.assertIn('info --format', runtime)
        self.assertIn("docker_unavailable", runtime)
        self.assertIn("docker_probe_timeout", runtime)
        self.assertIn("docker_recovered", runtime)
        self.assertIn("docker_timeout", runtime)
        self.assertIn("$Process.Kill()", runtime)
        self.assertNotIn("$process.WaitForExit()", runtime)
        self.assertNotIn("GetAwaiter().GetResult()", runtime)
        self.assertIn("Never target Docker Desktop", runtime)
        self.assertIn("Local\\J2M-SelfHost-Backup-Operations", runtime)
        self.assertIn("$global:LASTEXITCODE = $previousExitCode", runtime)
        self.assertIn("Send-J2MHealthcheck database_hourly start", runner)
        self.assertIn("Send-J2MHealthcheck database_hourly success", runner)
        self.assertIn("Send-J2MHealthcheck database_hourly fail", runner)
        self.assertIn("Send-J2MHealthcheck cloud_four_hour success", cloud)
        self.assertIn("Send-J2MHealthcheck restore_weekly success", restore)
        self.assertIn("Wait-J2MDockerReady -LogPath $runtimeLog", runner)
        self.assertIn("Wait-J2MDockerReady -LogPath $runtimeLog", restore)
        self.assertNotIn("Wait-J2MDockerReady", cloud)
        self.assertLess(runner.index("Enter-J2MSelfHostTaskGuard"), runner.index("Wait-J2MDockerReady"))
        self.assertLess(runner.index("Send-J2MHealthcheck database_hourly start"), runner.index("Wait-J2MDockerReady"))
        self.assertLess(runner.index("Wait-J2MDockerReady"), runner.index("database --env-file"))
        self.assertLess(restore.index("Enter-J2MSelfHostTaskGuard"), restore.index("Wait-J2MDockerReady"))
        self.assertLess(restore.index("Wait-J2MDockerReady"), restore.index("restore-isolated"))
        for task_runner in (runner, cloud, restore):
            self.assertIn("Enter-J2MSelfHostTaskGuard", task_runner)
            self.assertIn("Exit-J2MSelfHostTaskGuard", task_runner)
        self.assertLess(
            cloud.index("Set-Location -LiteralPath $Repository"),
            cloud.index("Send-J2MHealthcheck cloud_four_hour start"),
        )

    @unittest.skipUnless(os.name == "nt", "PowerShell integration only runs on Windows")
    def test_healthchecks_helper_is_best_effort_and_hides_secret_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret = root / "healthchecks.json"
            token = "unreachable-synthetic-token-value-1234"
            secret.write_text(
                json.dumps({"database_hourly": f"http://127.0.0.1:1/{token}"}),
                encoding="utf-8",
            )
            child_env = os.environ.copy()
            child_env["J2M_SYNTHETIC_HEALTHCHECK_TEST"] = "1"
            child_env["J2M_HEALTHCHECKS_FILE"] = str(secret)
            result = subprocess.run(
                [sys.executable, "-m", "scripts.healthchecks_monitor", "database_hourly", "fail"],
                capture_output=True, text=True, timeout=20, cwd=ROOT, env=child_env,
            )
            combined = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, combined)
            self.assertEqual(combined, "")
            self.assertNotIn(token, combined)
            from scripts.healthchecks_monitor import notify
            with patch.dict(os.environ, {"J2M_SYNTHETIC_HEALTHCHECK_TEST": "1"}):
                self.assertFalse(notify("database_hourly", "fail", secret))

    @unittest.skipUnless(os.name == "nt", "PowerShell integration only runs on Windows")
    def test_monitoring_process_failure_does_not_change_task_status(self):
        runtime = str(ROOT / "scripts" / "selfhost_task_runtime.ps1")
        command = (
            f". '{runtime}'; function python {{ throw 'synthetic monitor failure' }}; "
            "$global:LASTEXITCODE=91; Send-J2MHealthcheck database_hourly fail; "
            "if ($global:LASTEXITCODE -ne 91) { exit 3 }; Write-Output continued"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=20, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "continued")
        self.assertNotIn("synthetic monitor failure", result.stderr)

    @unittest.skipUnless(os.name == "nt", "PowerShell integration only runs on Windows")
    def test_docker_readiness_retries_then_records_recovery(self):
        runtime = str(ROOT / "scripts" / "selfhost_task_runtime.ps1")
        with tempfile.TemporaryDirectory() as temporary:
            log = str(Path(temporary) / "runtime.jsonl")
            command = (
                f". '{runtime}'; $script:checks=0; "
                "function Test-J2MDockerEngine { param($TimeoutSeconds); $script:checks += 1; "
                "if ($script:checks -ge 3) { return 'ready' }; return 'unavailable' }; "
                f"Wait-J2MDockerReady -LogPath '{log}' -MaxWaitSeconds 2 -RetryIntervalSeconds 0.05; "
                "Write-Output ('checks=' + $script:checks)"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=10, cwd=ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("checks=3", result.stdout)
            events = [json.loads(line) for line in Path(log).read_text(encoding="utf-8-sig").splitlines()]
            self.assertEqual([event["event"] for event in events], [
                "docker_unavailable", "docker_unavailable", "docker_recovered",
            ])
            self.assertEqual([event["status"] for event in events], ["retry", "retry", "ok"])
            self.assertEqual(events[-1]["attempt"], 3)

    @unittest.skipUnless(os.name == "nt", "PowerShell integration only runs on Windows")
    def test_docker_readiness_times_out_with_a_sanitized_event(self):
        runtime = str(ROOT / "scripts" / "selfhost_task_runtime.ps1")
        with tempfile.TemporaryDirectory() as temporary:
            log = str(Path(temporary) / "runtime.jsonl")
            command = (
                f". '{runtime}'; function Test-J2MDockerEngine {{ param($TimeoutSeconds); return 'unavailable' }}; "
                f"try {{ Wait-J2MDockerReady -LogPath '{log}' -MaxWaitSeconds 0.15 "
                "-RetryIntervalSeconds 0.04; exit 7 } catch { Write-Output timeout }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=10, cwd=ROOT,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "timeout")
            events = [json.loads(line) for line in Path(log).read_text(encoding="utf-8-sig").splitlines()]
            self.assertEqual(events[-1]["event"], "docker_timeout")
            self.assertEqual(events[-1]["status"], "error")
            self.assertTrue(all(set(event) == {"timestamp", "event", "status", "attempt"} for event in events))

    @unittest.skipUnless(os.name == "nt", "PowerShell integration only runs on Windows")
    def test_blocked_docker_probe_kills_only_the_probe_process(self):
        runtime = str(ROOT / "scripts" / "selfhost_task_runtime.ps1")
        with tempfile.TemporaryDirectory() as temporary:
            pid_file = Path(temporary) / "probe.pid"
            child_command = f"$PID | Set-Content -LiteralPath '{pid_file}' -Encoding ascii; Start-Sleep -Seconds 30"
            encoded = __import__("base64").b64encode(child_command.encode("utf-16-le")).decode("ascii")
            command = (
                f". '{runtime}'; $status=Invoke-J2MProcessWithTimeout -FileName 'powershell.exe' "
                f"-Arguments '-NoProfile -NonInteractive -EncodedCommand {encoded}' -TimeoutSeconds 2; "
                "Write-Output ('status=' + $status)"
            )
            started = time.monotonic()
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
                capture_output=True, text=True, timeout=10, cwd=ROOT,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "status=timeout")
            self.assertLess(elapsed, 8)
            probe_pid = int(pid_file.read_text(encoding="ascii").strip())
            alive = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", f"Get-Process -Id {probe_pid}"],
                capture_output=True, text=True, timeout=5,
            )
            self.assertNotEqual(alive.returncode, 0, alive.stdout + alive.stderr)

    @unittest.skipUnless(os.name == "nt", "PowerShell integration only runs on Windows")
    def test_probe_cleanup_uses_only_a_bounded_wait(self):
        runtime = str(ROOT / "scripts" / "selfhost_task_runtime.ps1")
        command = (
            f". '{runtime}'; "
            "$fake=[pscustomobject]@{ Killed=$false; WaitMilliseconds=0 }; "
            "$fake | Add-Member ScriptMethod Kill { $this.Killed=$true }; "
            "$fake | Add-Member ScriptMethod WaitForExit { param($milliseconds) "
            "$this.WaitMilliseconds=$milliseconds; Start-Sleep -Milliseconds 50; return $false }; "
            "$result=Stop-J2MProbeProcess -Process $fake -TimeoutMilliseconds 75; "
            "Write-Output ('killed=' + $fake.Killed + ';wait=' + $fake.WaitMilliseconds + ';result=' + $result)"
        )
        started = time.monotonic()
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=5, cwd=ROOT,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "killed=True;wait=75;result=False")
        self.assertLess(elapsed, 2)

    @unittest.skipUnless(os.name == "nt", "PowerShell integration only runs on Windows")
    def test_docker_probe_selects_one_executable_when_multiple_are_discovered(self):
        runtime = str(ROOT / "scripts" / "selfhost_task_runtime.ps1")
        command = (
            f". '{runtime}'; "
            "function Get-Command { param($Name, $CommandType, $ErrorAction); "
            "if ($Name -eq 'docker.exe') { return $null }; "
            "return @([pscustomobject]@{Source='first-docker.exe'}, "
            "[pscustomobject]@{Source='second-docker.exe'}) }; "
            "function Invoke-J2MProcessWithTimeout { param($FileName, $Arguments, $TimeoutSeconds); "
            "$script:selected=$FileName; return 'ready' }; "
            "$status=Test-J2MDockerEngine -TimeoutSeconds 10; "
            "Write-Output ('selected=' + $script:selected); Write-Output ('status=' + $status)"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", command],
            capture_output=True, text=True, timeout=5, cwd=ROOT,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip().splitlines(), ["selected=first-docker.exe", "status=ready"])

    @unittest.skipUnless(os.name == "nt", "PowerShell integration only runs on Windows")
    def test_task_guard_serializes_different_scheduled_runners(self):
        runtime = str(ROOT / "scripts" / "selfhost_task_runtime.ps1")
        mutex = "Local\\J2M-Synthetic-" + os.urandom(8).hex()
        holder_command = (
            f". '{runtime}'; $m=Enter-J2MSelfHostTaskGuard -TimeoutSeconds 5 -Name '{mutex}'; "
            "Write-Output locked; try { Start-Sleep -Milliseconds 1200 } "
            "finally { Exit-J2MSelfHostTaskGuard $m }"
        )
        waiter_command = (
            f". '{runtime}'; $m=Enter-J2MSelfHostTaskGuard -TimeoutSeconds 5 -Name '{mutex}'; "
            "try { Write-Output acquired } finally { Exit-J2MSelfHostTaskGuard $m }"
        )
        holder = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", holder_command],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT,
        )
        try:
            self.assertEqual(holder.stdout.readline().strip(), "locked")
            started = time.monotonic()
            waiter = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", waiter_command],
                capture_output=True, text=True, timeout=10, cwd=ROOT,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(waiter.returncode, 0, waiter.stdout + waiter.stderr)
            self.assertIn("acquired", waiter.stdout)
            self.assertGreaterEqual(elapsed, 0.8)
        finally:
            remaining_stdout, remaining_stderr = holder.communicate(timeout=10)
            self.assertEqual(holder.returncode, 0, remaining_stdout + remaining_stderr)

    @unittest.skipUnless(os.name == "nt", "PowerShell integration only runs on Windows")
    def test_healthchecks_helper_sends_start_success_and_fail(self):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(self.path)
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                secret = Path(temporary) / "healthchecks.json"
                token_path = "/synthetic-secret-token-value-1234"
                secret.write_text(json.dumps({
                    "database_hourly": f"http://127.0.0.1:{server.server_port}{token_path}",
                }), encoding="utf-8")
                from scripts.healthchecks_monitor import notify
                with patch.dict(os.environ, {"J2M_SYNTHETIC_HEALTHCHECK_TEST": "1"}):
                    for state in ("start", "success", "fail"):
                        self.assertTrue(notify("database_hourly", state, secret))
                self.assertEqual(received, [
                    token_path + "/start", token_path, token_path + "/fail",
                ])
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_healthchecks_helper_accepts_utf8_bom_and_rejects_unsafe_urls(self):
        from scripts.healthchecks_monitor import notify

        with tempfile.TemporaryDirectory() as temporary:
            secret = Path(temporary) / "healthchecks.json"
            secret.write_text(
                json.dumps({"database_hourly": "https://hc-ping.com/" + "a" * 32}),
                encoding="utf-8-sig",
            )
            with patch("scripts.healthchecks_monitor.urlopen") as opener:
                response = opener.return_value.__enter__.return_value
                response.status = 200
                self.assertTrue(notify("database_hourly", "success", secret))

            for unsafe in (
                "http://hc-ping.com/" + "a" * 32,
                "https://user@hc-ping.com/" + "a" * 32,
                "https://hc-ping.com/" + "a" * 32 + "?leak=1",
                "https://hc-ping.com/short",
                "https://example.com/" + "a" * 32,
            ):
                secret.write_text(json.dumps({"database_hourly": unsafe}), encoding="utf-8")
                with patch("scripts.healthchecks_monitor.urlopen") as opener:
                    self.assertFalse(notify("database_hourly", "success", secret))
                    opener.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "ACL y Task Scheduler solo se validan en Windows")
    def test_windows_acl_and_scheduler_plan_execute_without_registering_tasks(self):
        (ROOT / "selfhost-backups").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=ROOT / "selfhost-backups") as temporary:
            secret = Path(temporary) / "secret.txt"
            secret.write_text("synthetic", encoding="utf-8")
            secret_directory = Path(temporary) / "private"
            secret_directory.mkdir()
            for protected_path in (secret, secret_directory):
                acl_result = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                     str(ROOT / "scripts" / "protect_selfhost_secrets.ps1"), "-Path",
                     str(protected_path)],
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
