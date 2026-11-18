"""Encrypted, non-overlapping backups for the Windows self-hosted deployment.

The database stream is authenticated in independent AES-GCM frames before any
backup bytes are written.  Production keys are read from a CurrentUser DPAPI
blob; command lines and logs never contain the key or database URL.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import struct
import subprocess
from typing import BinaryIO, Iterator
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from scripts.portable_backup import _password, _portable_text, _protect_dpapi, _unprotect_dpapi, decrypt_bytes, encrypt_bytes


MAGIC = b"J2MSTREAM1"
CHUNK_SIZE = 1024 * 1024
NONCE_PREFIX_BYTES = 8


def encrypt_stream(source: BinaryIO, destination: BinaryIO, key: bytes) -> str:
    if len(key) != 32:
        raise ValueError("La clave de respaldo debe contener 32 bytes.")
    nonce_prefix = secrets.token_bytes(NONCE_PREFIX_BYTES)
    header = MAGIC + nonce_prefix
    destination.write(header)
    digest = hashlib.sha256(header)
    cipher = AESGCM(key)
    index = 0
    while True:
        cleartext = source.read(CHUNK_SIZE)
        if not cleartext:
            break
        nonce = nonce_prefix + index.to_bytes(4, "big")
        aad = header + index.to_bytes(4, "big")
        ciphertext = cipher.encrypt(nonce, cleartext, aad)
        frame = struct.pack(">I", len(ciphertext)) + ciphertext
        destination.write(frame)
        digest.update(frame)
        index += 1
        if index >= 2**32:
            raise ValueError("El flujo excede el límite de marcos cifrados.")
    terminator = struct.pack(">I", 0)
    destination.write(terminator)
    digest.update(terminator)
    return digest.hexdigest()


def decrypt_stream(source: BinaryIO, destination: BinaryIO, key: bytes) -> None:
    header = source.read(len(MAGIC) + NONCE_PREFIX_BYTES)
    if not header.startswith(MAGIC) or len(header) != len(MAGIC) + NONCE_PREFIX_BYTES:
        raise ValueError("Formato de respaldo cifrado no reconocido.")
    nonce_prefix = header[len(MAGIC) :]
    cipher = AESGCM(key)
    index = 0
    while True:
        length_raw = source.read(4)
        if len(length_raw) != 4:
            raise ValueError("Respaldo cifrado truncado.")
        length = struct.unpack(">I", length_raw)[0]
        if length == 0:
            if source.read(1):
                raise ValueError("Datos inesperados después del terminador.")
            return
        ciphertext = source.read(length)
        if len(ciphertext) != length:
            raise ValueError("Respaldo cifrado truncado.")
        nonce = nonce_prefix + index.to_bytes(4, "big")
        aad = header + index.to_bytes(4, "big")
        destination.write(cipher.decrypt(nonce, ciphertext, aad))
        index += 1


def verify_sha256(path: Path) -> bool:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    expected = sidecar.read_text(encoding="ascii").strip().split()[0].lower()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return secrets.compare_digest(expected, digest.hexdigest())


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_dpapi_key(path: Path) -> bytes:
    key = _portable_text(_unprotect_dpapi(path.read_bytes()))
    if len(key) != 32:
        raise ValueError("La clave DPAPI no contiene una clave AES-256 válida.")
    return key


def initialize_key(key_file: Path, recovery_file: Path) -> None:
    if key_file.exists() or recovery_file.exists():
        raise ValueError("No se sobrescribirá una clave o recuperación existente.")
    key_file.parent.mkdir(parents=True, exist_ok=True)
    recovery_file.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    password = _password(confirm=True)
    recovery_payload = encrypt_bytes(key, password)
    key_file.write_bytes(_protect_dpapi(key))
    recovery_file.write_bytes(recovery_payload)
    if decrypt_bytes(recovery_payload, password) != key or load_dpapi_key(key_file) != key:
        key_file.unlink(missing_ok=True)
        recovery_file.unlink(missing_ok=True)
        raise RuntimeError("No fue posible verificar la custodia de la clave.")


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.tell() == handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except OSError as exc:
        raise RuntimeError("Ya existe una ejecución de respaldo activa.") from exc
    finally:
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _safe_log(log_path: Path, event: str, status: str, detail: str = "") -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "status": status,
        "detail": detail[:300],
    }
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, sort_keys=True) + "\n")


def database_backup(
    env_file: Path, output_dir: Path, key_file: Path, retries: int = 3, network: str = "bridge"
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    key = load_dpapi_key(key_file)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = output_dir / f"j2m-postgres-{timestamp}.dump.j2mbackup"
    partial = final.with_suffix(final.suffix + ".partial")
    log_path = output_dir.parent / "logs" / "backup.jsonl"
    command = [
        "docker", "run", "--rm", "--network", network, "--env-file", str(env_file),
        "postgres:17-alpine", "sh", "-eu", "-c",
        'pg_dump --format=custom --no-owner --no-acl "$DATABASE_URL"',
    ]
    with exclusive_lock(output_dir.parent / ".backup.lock"):
        for attempt in range(1, retries + 1):
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert process.stdout is not None
            try:
                with partial.open("xb") as encrypted:
                    encrypt_stream(process.stdout, encrypted, key)
                stderr = process.stderr.read() if process.stderr else b""
                code = process.wait()
                if code:
                    raise RuntimeError(f"pg_dump terminó con código {code}")
                digest = _hash_file(partial)
                partial.replace(final)
                final.with_suffix(final.suffix + ".sha256").write_text(
                    f"{digest}  {final.name}\n", encoding="ascii"
                )
                if not verify_sha256(final):
                    raise RuntimeError("La verificación SHA-256 posterior a la escritura falló.")
                _safe_log(log_path, "database_backup", "ok", f"attempt={attempt}")
                return final
            except Exception as exc:
                process.kill()
                process.wait()
                partial.unlink(missing_ok=True)
                _safe_log(log_path, "database_backup", "retry" if attempt < retries else "error", type(exc).__name__)
                if attempt == retries:
                    raise
    raise RuntimeError("No fue posible completar el respaldo.")


def retain_local(directory: Path, keep: int = 48) -> int:
    backups = sorted(directory.glob("*.j2mbackup"), key=lambda item: item.stat().st_mtime, reverse=True)
    removed = 0
    for item in backups[keep:]:
        item.with_suffix(item.suffix + ".sha256").unlink(missing_ok=True)
        item.unlink()
        removed += 1
    return removed


def configuration_backup(paths: list[Path], output_dir: Path, key_file: Path) -> Path | None:
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
        return None
    import io

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in existing:
            bundle.writestr(path.name, path.read_bytes())
    archive.seek(0)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = output_dir / f"j2m-configuration-{timestamp}.zip.j2mbackup"
    with final.open("xb") as destination:
        digest = encrypt_stream(archive, destination, load_dpapi_key(key_file))
    final.with_suffix(final.suffix + ".sha256").write_text(f"{digest}  {final.name}\n", encoding="ascii")
    state_path.write_text(current, encoding="ascii")
    return final


def restore_isolated(backup: Path, key_file: Path) -> str:
    if not verify_sha256(backup):
        raise ValueError("La suma SHA-256 no coincide.")
    name = "j2m-restore-" + secrets.token_hex(6)
    password = secrets.token_urlsafe(24)
    run = [
        "docker", "run", "-d", "--name", name, "--network", "none",
        "--tmpfs", "/var/lib/postgresql/data:rw,noexec,nosuid,size=2g",
        "-e", "POSTGRES_DB=j2m_restore", "-e", "POSTGRES_USER=j2m_restore",
        "-e", f"POSTGRES_PASSWORD={password}", "postgres:17-alpine",
    ]
    subprocess.run(run, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ready = False
        for _ in range(30):
            probe = subprocess.run(
                ["docker", "exec", name, "pg_isready", "-U", "j2m_restore", "-d", "j2m_restore"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            if probe.returncode == 0:
                ready = True
                break
            import time
            time.sleep(1)
        if not ready:
            raise RuntimeError("PostgreSQL aislado no quedó disponible.")
        restore = subprocess.Popen(
            ["docker", "exec", "-i", name, "pg_restore", "--exit-on-error", "--no-owner", "--no-acl",
             "-U", "j2m_restore", "-d", "j2m_restore"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        assert restore.stdin is not None
        with backup.open("rb") as encrypted:
            decrypt_stream(encrypted, restore.stdin, load_dpapi_key(key_file))
        restore.stdin.close()
        stderr = restore.stderr.read() if restore.stderr else b""
        if restore.wait() != 0:
            raise RuntimeError("pg_restore falló en la base aislada.")
        query = subprocess.run(
            ["docker", "exec", name, "psql", "-U", "j2m_restore", "-d", "j2m_restore", "-Atqc",
             "SELECT version_num FROM alembic_version"],
            check=True, capture_output=True, text=True,
        )
        return query.stdout.strip()
    finally:
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("database")
    backup.add_argument("--env-file", type=Path, default=Path(".env.selfhost"))
    backup.add_argument("--output-dir", type=Path, default=Path("selfhost-backups/hourly"))
    backup.add_argument("--key-file", type=Path, required=True)
    backup.add_argument("--retries", type=int, default=3)
    backup.add_argument("--network", default="bridge", help=argparse.SUPPRESS)
    verify = subparsers.add_parser("verify")
    verify.add_argument("backup", type=Path)
    prune = subparsers.add_parser("retain-local")
    prune.add_argument("directory", type=Path)
    prune.add_argument("--keep", type=int, default=48)
    initialize = subparsers.add_parser("init-key")
    initialize.add_argument("--key-file", type=Path, required=True)
    initialize.add_argument("--recovery-file", type=Path, required=True)
    config = subparsers.add_parser("configuration")
    config.add_argument("--path", type=Path, action="append", required=True)
    config.add_argument("--output-dir", type=Path, default=Path("selfhost-backups/configuration"))
    config.add_argument("--key-file", type=Path, required=True)
    restore = subparsers.add_parser("restore-isolated")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--key-file", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "database":
        path = database_backup(
            args.env_file, args.output_dir, args.key_file, max(1, min(args.retries, 6)), args.network
        )
        retain_local(args.output_dir)
        print(f"Respaldo cifrado creado y verificado: {path.name}")
        return 0
    if args.command == "verify":
        if not verify_sha256(args.backup):
            raise ValueError("La suma SHA-256 no coincide.")
        print("Integridad SHA-256 verificada.")
        return 0
    if args.command == "init-key":
        initialize_key(args.key_file, args.recovery_file)
        print("Clave DPAPI y recuperación portable cifrada creadas y verificadas.")
        return 0
    if args.command == "configuration":
        path = configuration_backup(args.path, args.output_dir, args.key_file)
        print("Configuración sin cambios." if path is None else f"Configuración cifrada: {path.name}")
        return 0
    if args.command == "restore-isolated":
        revision = restore_isolated(args.backup, args.key_file)
        print(f"Restauración aislada verificada; Alembic={revision}")
        return 0
    print(f"Respaldos eliminados por retención: {retain_local(args.directory, args.keep)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
