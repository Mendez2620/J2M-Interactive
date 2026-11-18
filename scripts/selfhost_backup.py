"""Authenticated and recoverable backups for the Windows self-hosted deployment."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import struct
import subprocess
import threading
import time
from typing import BinaryIO, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.staging_preflight import EXPECTED_ALEMBIC_HEAD
from scripts.portable_backup import (
    _password, _protect_dpapi, _unprotect_dpapi, decrypt_bytes, encrypt_bytes,
)


MAGIC = b"J2MSTREAM2"
CHUNK_SIZE = 1024 * 1024
NONCE_PREFIX_BYTES = 8
TAG_BYTES = 16
DATA = b"D"
END = b"E"
MAX_FRAME_BYTES = CHUNK_SIZE + TAG_BYTES
END_CIPHERTEXT_BYTES = 16 + TAG_BYTES


class _NullWriter:
    def write(self, value: bytes) -> int:
        return len(value)


def _nonce(prefix: bytes, index: int) -> bytes:
    if not 0 <= index < 2**32:
        raise ValueError("El flujo excede el límite de marcos cifrados.")
    return prefix + index.to_bytes(4, "big")


def _aad(header: bytes, frame_type: bytes, index: int) -> bytes:
    return header + frame_type + index.to_bytes(4, "big")


def encrypt_stream(source: BinaryIO, destination: BinaryIO, key: bytes) -> str:
    """Encrypt data frames and an authenticated final frame with counts and size."""
    if len(key) != 32:
        raise ValueError("La clave de respaldo debe contener 32 bytes.")
    nonce_prefix = secrets.token_bytes(NONCE_PREFIX_BYTES)
    header = MAGIC + nonce_prefix + struct.pack(">I", CHUNK_SIZE)
    destination.write(header)
    digest = hashlib.sha256(header)
    cipher = AESGCM(key)
    frame_count = 0
    total_plaintext = 0
    while True:
        cleartext = source.read(CHUNK_SIZE)
        if not cleartext:
            break
        if len(cleartext) > CHUNK_SIZE:
            raise ValueError("El origen devolvió un marco mayor que el permitido.")
        ciphertext = cipher.encrypt(
            _nonce(nonce_prefix, frame_count), cleartext, _aad(header, DATA, frame_count)
        )
        record = DATA + struct.pack(">I", len(ciphertext)) + ciphertext
        destination.write(record)
        digest.update(record)
        frame_count += 1
        total_plaintext += len(cleartext)
    final_ciphertext = cipher.encrypt(
        _nonce(nonce_prefix, frame_count),
        struct.pack(">QQ", frame_count, total_plaintext),
        _aad(header, END, frame_count),
    )
    record = END + struct.pack(">I", len(final_ciphertext)) + final_ciphertext
    destination.write(record)
    digest.update(record)
    return digest.hexdigest()


def decrypt_stream(source: BinaryIO, destination: BinaryIO, key: bytes) -> tuple[int, int]:
    """Reject truncation, deletion, reordering, malformed sizes and unauthenticated tails."""
    if len(key) != 32:
        raise ValueError("La clave de respaldo debe contener 32 bytes.")
    header_size = len(MAGIC) + NONCE_PREFIX_BYTES + 4
    header = source.read(header_size)
    if len(header) != header_size or not header.startswith(MAGIC):
        raise ValueError("Formato de respaldo cifrado no reconocido.")
    if struct.unpack(">I", header[-4:])[0] != CHUNK_SIZE:
        raise ValueError("Tamaño de marco no admitido.")
    prefix = header[len(MAGIC) : len(MAGIC) + NONCE_PREFIX_BYTES]
    cipher = AESGCM(key)
    frame_count = 0
    total_plaintext = 0
    while True:
        frame_type = source.read(1)
        if not frame_type:
            raise ValueError("Respaldo truncado antes del terminador autenticado.")
        length_raw = source.read(4)
        if len(length_raw) != 4:
            raise ValueError("Respaldo cifrado truncado.")
        length = struct.unpack(">I", length_raw)[0]
        if frame_type == DATA:
            if not TAG_BYTES <= length <= MAX_FRAME_BYTES:
                raise ValueError("Tamaño de marco cifrado inválido.")
            ciphertext = source.read(length)
            if len(ciphertext) != length:
                raise ValueError("Respaldo cifrado truncado.")
            cleartext = cipher.decrypt(
                _nonce(prefix, frame_count), ciphertext, _aad(header, DATA, frame_count)
            )
            if not cleartext or len(cleartext) > CHUNK_SIZE:
                raise ValueError("Contenido de marco inválido.")
            destination.write(cleartext)
            frame_count += 1
            total_plaintext += len(cleartext)
            continue
        if frame_type != END or length != END_CIPHERTEXT_BYTES:
            raise ValueError("Tipo o tamaño de terminador inválido.")
        ciphertext = source.read(length)
        if len(ciphertext) != length:
            raise ValueError("Terminador autenticado truncado.")
        final_plaintext = cipher.decrypt(
            _nonce(prefix, frame_count), ciphertext, _aad(header, END, frame_count)
        )
        expected_frames, expected_total = struct.unpack(">QQ", final_plaintext)
        if expected_frames != frame_count or expected_total != total_plaintext:
            raise ValueError("El recuento o la longitud autenticados no coinciden.")
        if source.read(1):
            raise ValueError("Datos inesperados después del terminador autenticado.")
        return frame_count, total_plaintext


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".sha256")


def _manifest(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".complete.json")


def verify_sha256(path: Path) -> bool:
    try:
        expected = _sidecar(path).read_text(encoding="ascii").strip().split()[0].lower()
    except (FileNotFoundError, IndexError):
        return False
    return secrets.compare_digest(expected, _hash_file(path))


def _verify_manifest(path: Path) -> bool:
    try:
        payload = json.loads(_manifest(path).read_text(encoding="utf-8"))
        return (
            payload["filename"] == path.name
            and payload["sha256"] == _hash_file(path)
            and payload["size"] == path.stat().st_size
            and verify_sha256(path)
        )
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError, OSError):
        return False


def verify_backup(path: Path, key: bytes | None = None) -> bool:
    if not _verify_manifest(path):
        return False
    if key is None:
        return True
    try:
        with path.open("rb") as source:
            decrypt_stream(source, _NullWriter(), key)
        return True
    except Exception:
        return False


def load_dpapi_key(path: Path) -> bytes:
    """DPAPI key material is binary and must never pass through text normalization."""
    key = _unprotect_dpapi(path.read_bytes())
    if len(key) != 32:
        raise ValueError("La clave DPAPI no contiene una clave AES-256 válida.")
    return key


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial-" + secrets.token_hex(4))
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def initialize_key(key_file: Path, recovery_file: Path) -> None:
    if key_file.exists() or recovery_file.exists():
        raise ValueError("No se sobrescribirá una clave o recuperación existente.")
    key = secrets.token_bytes(32)
    password = _password(confirm=True)
    payload = encrypt_bytes(key, password)
    try:
        _atomic_write(key_file, _protect_dpapi(key))
        _atomic_write(recovery_file, payload)
        if decrypt_bytes(payload, password) != key or load_dpapi_key(key_file) != key:
            raise RuntimeError("No fue posible verificar la custodia de la clave.")
    except Exception:
        key_file.unlink(missing_ok=True)
        recovery_file.unlink(missing_ok=True)
        raise


def recover_key(recovery_file: Path, key_file: Path) -> None:
    """Create a new-profile DPAPI key from the portable password envelope."""
    if key_file.exists():
        raise ValueError("No se sobrescribirá una clave DPAPI existente.")
    key = decrypt_bytes(recovery_file.read_bytes(), _password(confirm=False))
    if len(key) != 32:
        raise ValueError("La recuperación portable no contiene una clave AES-256 válida.")
    try:
        _atomic_write(key_file, _protect_dpapi(key))
        if load_dpapi_key(key_file) != key:
            raise RuntimeError("La clave recuperada no superó la verificación.")
    except Exception:
        key_file.unlink(missing_ok=True)
        raise


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    locked = False
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError as exc:
        raise RuntimeError("Ya existe una operación de respaldo activa.") from exc
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _safe_log(path: Path, event: str, status: str, detail: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event,
              "status": status, "detail": detail[:160]}
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def _read_database_url(env_file: Path, allow_insecure_synthetic: bool = False) -> str:
    value = None
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("DATABASE_URL="):
            value = line.partition("=")[2].strip().strip("'\"")
            break
    if not value:
        raise ValueError("DATABASE_URL no está disponible.")
    parsed = urlsplit(value)
    if parsed.scheme not in {"postgres", "postgresql"} or not parsed.hostname:
        raise ValueError("DATABASE_URL no es PostgreSQL válida.")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if allow_insecure_synthetic:
        if os.environ.get("J2M_SYNTHETIC_BACKUP_TEST") != "1" or parsed.hostname != "j2m-backup-source":
            raise ValueError("La excepción TLS solo se admite para el ensayo sintético aislado.")
    else:
        query["sslmode"] = "verify-full"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _publish_bundle(partial: Path, final: Path, kind: str) -> None:
    digest = _hash_file(partial)
    size = partial.stat().st_size
    partial.replace(final)
    _atomic_write(_sidecar(final), f"{digest}  {final.name}\n".encode("ascii"))
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "filename": final.name,
                "kind": kind, "sha256": digest, "size": size}
    _atomic_write(_manifest(final), json.dumps(manifest, sort_keys=True).encode("utf-8"))
    if not _verify_manifest(final):
        raise RuntimeError("La publicación atómica no superó la verificación.")


def _complete_backups(directory: Path) -> list[Path]:
    return sorted((p for p in directory.glob("*.j2mbackup") if _verify_manifest(p)),
                  key=lambda item: item.stat().st_mtime, reverse=True)


def _retain_local_locked(directory: Path, keep: int) -> int:
    backups = _complete_backups(directory)
    removed = 0
    for item in backups[max(1, keep):]:
        _manifest(item).unlink(missing_ok=True)
        _sidecar(item).unlink(missing_ok=True)
        item.unlink(missing_ok=True)
        removed += 1
    return removed


def retain_local(directory: Path, keep: int = 48, lock_file: Path | None = None) -> int:
    with exclusive_lock(lock_file or directory.parent / ".operations.lock"):
        return _retain_local_locked(directory, keep)


def _run_streaming_dump(command: list[str], child_env: dict[str, str], output: Path,
                        key: bytes, timeout: int) -> None:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env)
    assert process.stdout is not None
    timed_out = threading.Event()
    def terminate() -> None:
        timed_out.set()
        process.kill()
    timer = threading.Timer(timeout, terminate)
    timer.start()
    try:
        with output.open("xb") as encrypted:
            encrypt_stream(process.stdout, encrypted, key)
            encrypted.flush()
            os.fsync(encrypted.fileno())
        if process.wait(timeout=10) != 0:
            raise RuntimeError("pg_dump no pudo completar el respaldo.")
        if timed_out.is_set():
            raise TimeoutError("pg_dump excedió el tiempo máximo.")
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait()


def database_backup(env_file: Path, output_dir: Path, key_file: Path, retries: int = 3,
                    network: str = "bridge", timeout: int = 900,
                    allow_insecure_synthetic: bool = False) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    key = load_dpapi_key(key_file)
    database_url = _read_database_url(env_file, allow_insecure_synthetic)
    lock_file = output_dir.parent / ".operations.lock"
    log_path = output_dir.parent / "logs" / "backup.jsonl"
    with exclusive_lock(lock_file):
        for attempt in range(1, retries + 1):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            final = output_dir / f"j2m-postgres-{stamp}-{secrets.token_hex(3)}.dump.j2mbackup"
            partial = final.with_name(final.name + ".partial")
            container = "j2m-pgdump-" + secrets.token_hex(6)
            command = ["docker", "run", "--rm", "--name", container, "--network", network,
                       "-e", "DATABASE_URL", "postgres:17-alpine", "sh", "-eu", "-c",
                       'pg_dump --format=custom --no-owner --no-acl "$DATABASE_URL"']
            child_env = os.environ.copy()
            child_env["DATABASE_URL"] = database_url
            try:
                _run_streaming_dump(command, child_env, partial, key, timeout)
                _publish_bundle(partial, final, "postgresql")
                if not verify_backup(final, key):
                    raise RuntimeError("El respaldo publicado no superó autenticación completa.")
                _retain_local_locked(output_dir, 48)
                _safe_log(log_path, "database_backup", "ok", f"attempt={attempt}")
                return final
            except Exception as exc:
                subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=30)
                partial.unlink(missing_ok=True)
                _safe_log(log_path, "database_backup", "retry" if attempt < retries else "error",
                          type(exc).__name__)
                if attempt == retries:
                    raise
    raise RuntimeError("No fue posible completar el respaldo.")


def configuration_backup(paths: list[Path], output_dir: Path, key_file: Path,
                         lock_file: Path | None = None) -> Path | None:
    with exclusive_lock(lock_file or output_dir.parent / ".operations.lock"):
        existing = [path for path in paths if path.is_file()]
        if not existing:
            raise ValueError("No existen archivos privados para respaldar.")
        fingerprint = hashlib.sha256()
        for path in sorted(existing, key=lambda item: str(item)):
            fingerprint.update(path.name.encode("utf-8"))
            fingerprint.update(path.read_bytes())
        state_path = output_dir / ".configuration.sha256"
        current = fingerprint.hexdigest()
        if state_path.exists() and state_path.read_text(encoding="ascii").strip() == current:
            if not _complete_backups(output_dir):
                raise RuntimeError("La configuración no cambió, pero falta un respaldo completo válido.")
            return None
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for path in existing:
                bundle.writestr(path.name, path.read_bytes())
        archive.seek(0)
        output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        final = output_dir / f"j2m-configuration-{stamp}-{secrets.token_hex(3)}.zip.j2mbackup"
        partial = final.with_name(final.name + ".partial")
        with partial.open("xb") as destination:
            encrypt_stream(archive, destination, load_dpapi_key(key_file))
            destination.flush()
            os.fsync(destination.fileno())
        _publish_bundle(partial, final, "configuration")
        _atomic_write(state_path, current.encode("ascii"))
        _retain_local_locked(output_dir, 30)
        return final


def stage_offsite(source_dir: Path, destination_dir: Path, key_file: Path,
                  lock_file: Path | None = None) -> Path:
    """Stage exactly the newest complete, SHA and AEAD verified bundle."""
    with exclusive_lock(lock_file or source_dir.parent / ".operations.lock"):
        key = load_dpapi_key(key_file)
        selected = next((p for p in _complete_backups(source_dir) if verify_backup(p, key)), None)
        if selected is None:
            raise RuntimeError("No existe un respaldo completo y autenticado para transferir.")
        staging = destination_dir.with_name(destination_dir.name + ".partial-" + secrets.token_hex(4))
        staging.mkdir(parents=True)
        try:
            for item in (selected, _sidecar(selected), _manifest(selected)):
                shutil.copy2(item, staging / item.name)
            copied = staging / selected.name
            if not verify_backup(copied, key):
                raise RuntimeError("La copia preparada no superó la verificación completa.")
            old = destination_dir.with_name(destination_dir.name + ".old")
            if old.exists():
                shutil.rmtree(old)
            if destination_dir.exists():
                destination_dir.replace(old)
            staging.replace(destination_dir)
            if old.exists():
                shutil.rmtree(old)
            return destination_dir / selected.name
        finally:
            if staging.exists():
                shutil.rmtree(staging)


@dataclass(frozen=True)
class RestoreReport:
    alembic_revision: str
    businesses: int
    customers: int
    staff: int
    transactions: int


def _psql(name: str, sql: str, timeout: int = 30) -> str:
    result = subprocess.run(["docker", "exec", name, "psql", "-U", "j2m_restore", "-d",
                             "j2m_restore", "-Atqc", sql], check=True, capture_output=True,
                            text=True, timeout=timeout)
    return result.stdout.strip()


def restore_isolated(backup: Path, key_file: Path, lock_file: Path | None = None,
                     timeout: int = 900) -> RestoreReport:
    with exclusive_lock(lock_file or backup.parent / ".operations.lock"):
        key = load_dpapi_key(key_file)
        if not verify_backup(backup, key):
            raise ValueError("El respaldo no superó SHA-256, manifiesto y autenticación AES-GCM.")
        name = "j2m-restore-" + secrets.token_hex(6)
        password = secrets.token_urlsafe(24)
        subprocess.run(["docker", "run", "-d", "--name", name, "--network", "none",
                        "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid,size=2g",
                        "-e", "POSTGRES_DB=j2m_restore", "-e", "POSTGRES_USER=j2m_restore",
                        "-e", f"POSTGRES_PASSWORD={password}", "postgres:17-alpine"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        try:
            for _ in range(30):
                probe = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "j2m_restore",
                                        "-d", "j2m_restore"], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=10)
                if probe.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("PostgreSQL aislado no quedó disponible.")
            restore = subprocess.Popen(["docker", "exec", "-i", name, "pg_restore",
                                        "--exit-on-error", "--no-owner", "--no-acl", "-U",
                                        "j2m_restore", "-d", "j2m_restore"], stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            assert restore.stdin is not None
            timer = threading.Timer(timeout, restore.kill)
            timer.start()
            try:
                with backup.open("rb") as encrypted:
                    decrypt_stream(encrypted, restore.stdin, key)
                restore.stdin.close()
                if restore.wait(timeout=10) != 0:
                    raise RuntimeError("pg_restore falló en la base aislada.")
            finally:
                timer.cancel()
                if restore.poll() is None:
                    restore.kill()
                    restore.wait()
            revision = _psql(name, "SELECT version_num FROM alembic_version")
            if revision != EXPECTED_ALEMBIC_HEAD:
                raise RuntimeError("La restauración no coincide con el head Alembic esperado.")
            missing_sql = (
                "SELECT count(*) FROM (VALUES (to_regclass('public.businesses'))," +
                "(to_regclass('public.customers')),(to_regclass('public.staff'))," +
                "(to_regclass('public.transactions'))) AS r(t) WHERE t IS NULL"
            )
            missing = _psql(name, missing_sql)
            if missing != "0":
                raise RuntimeError("Faltan tablas representativas en la restauración.")
            orphans = _psql(name, "SELECT count(*) FROM customers c LEFT JOIN businesses b "
                             "ON b.id=c.business_id WHERE b.id IS NULL")
            invalid = _psql(name, "SELECT count(*) FROM customers WHERE current_points < 0 "
                             "OR current_stamps < 0")
            if orphans != "0" or invalid != "0":
                raise RuntimeError("La restauración incumple invariantes representativas.")
            counts = [int(v) for v in _psql(name, "SELECT (SELECT count(*) FROM businesses)||','||"
                      "(SELECT count(*) FROM customers)||','||(SELECT count(*) FROM staff)||','||"
                      "(SELECT count(*) FROM transactions)").split(",")]
            return RestoreReport(revision, *counts)
        finally:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("database")
    backup.add_argument("--env-file", type=Path, default=Path(".env.selfhost"))
    backup.add_argument("--output-dir", type=Path, default=Path("selfhost-backups/hourly"))
    backup.add_argument("--key-file", type=Path, required=True)
    backup.add_argument("--retries", type=int, default=3)
    backup.add_argument("--timeout", type=int, default=900)
    backup.add_argument("--network", default="bridge", help=argparse.SUPPRESS)
    backup.add_argument("--allow-insecure-synthetic", action="store_true", help=argparse.SUPPRESS)
    verify = commands.add_parser("verify")
    verify.add_argument("backup", type=Path)
    verify.add_argument("--key-file", type=Path, required=True)
    prune = commands.add_parser("retain-local")
    prune.add_argument("directory", type=Path)
    prune.add_argument("--keep", type=int, default=48)
    prune.add_argument("--lock-file", type=Path)
    initialize = commands.add_parser("init-key")
    initialize.add_argument("--key-file", type=Path, required=True)
    initialize.add_argument("--recovery-file", type=Path, required=True)
    recover = commands.add_parser("recover-key")
    recover.add_argument("--key-file", type=Path, required=True)
    recover.add_argument("--recovery-file", type=Path, required=True)
    config = commands.add_parser("configuration")
    config.add_argument("--path", type=Path, action="append", required=True)
    config.add_argument("--output-dir", type=Path, default=Path("selfhost-backups/configuration"))
    config.add_argument("--key-file", type=Path, required=True)
    stage = commands.add_parser("stage-offsite")
    stage.add_argument("--source-dir", type=Path, required=True)
    stage.add_argument("--destination-dir", type=Path, required=True)
    stage.add_argument("--key-file", type=Path, required=True)
    stage.add_argument("--lock-file", type=Path)
    restore = commands.add_parser("restore-isolated")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--key-file", type=Path, required=True)
    restore.add_argument("--lock-file", type=Path)
    restore.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    if args.command == "database":
        path = database_backup(args.env_file, args.output_dir, args.key_file,
                               max(1, min(args.retries, 6)), args.network,
                               max(30, min(args.timeout, 7200)), args.allow_insecure_synthetic)
        print(f"Respaldo cifrado creado y verificado: {path.name}")
    elif args.command == "verify":
        if not verify_backup(args.backup, load_dpapi_key(args.key_file)):
            raise ValueError("El respaldo no superó la verificación completa.")
        print("Integridad SHA-256, manifiesto y AES-GCM verificada.")
    elif args.command == "init-key":
        initialize_key(args.key_file, args.recovery_file)
        print("Clave DPAPI y recuperación portable cifrada creadas y verificadas.")
    elif args.command == "recover-key":
        recover_key(args.recovery_file, args.key_file)
        print("Clave DPAPI recuperada y verificada en el perfil actual.")
    elif args.command == "configuration":
        path = configuration_backup(args.path, args.output_dir, args.key_file)
        print("Configuración sin cambios." if path is None else f"Configuración cifrada: {path.name}")
    elif args.command == "stage-offsite":
        path = stage_offsite(args.source_dir, args.destination_dir, args.key_file, args.lock_file)
        print(f"Copia verificada preparada: {path.name}")
    elif args.command == "restore-isolated":
        report = restore_isolated(args.backup, args.key_file, args.lock_file, args.timeout)
        print(f"Restauración aislada verificada; Alembic={report.alembic_revision}; "
              "tablas=4; recuentos comprobados")
    else:
        print(f"Respaldos eliminados por retención: "
              f"{retain_local(args.directory, args.keep, args.lock_file)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
