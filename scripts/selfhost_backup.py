"""Authenticated and recoverable backups for the Windows self-hosted deployment."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import struct
import subprocess
import sys
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
MEDIA_MAGIC = b"J2MMEDIA1"
MEDIA_MAX_FILES = 10000
MEDIA_MAX_FILE_BYTES = 25 * 1024 * 1024
MEDIA_MAX_TOTAL_BYTES = 5 * 1024 * 1024 * 1024
MEDIA_KEY_RE = re.compile(
    r"businesses/[1-9]\d*/(?:logo|cover)/[0-9a-f]{32}\.(?:png|jpg|webp)"
)
MEDIA_MARKER_RE = re.compile(
    r"businesses/[1-9]\d*/(?:logo|cover)/\.[0-9a-f]{32}\.(?:png|jpg|webp)\.retained\.json"
)


class _NullWriter:
    def write(self, value: bytes) -> int:
        return len(value)

    def close(self) -> None:
        return None


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


def _read_env_value(env_file: Path, name: str) -> str | None:
    prefix = name + "="
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            return line.partition("=")[2].strip().strip("'\"")
    return None


def _read_database_url(env_file: Path, allow_insecure_synthetic: bool = False,
                       sslrootcert: str | None = None) -> str:
    value = _read_env_value(env_file, "DATABASE_URL")
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
        query["sslrootcert"] = sslrootcert or "system"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _publish_bundle(partial: Path, final: Path, kind: str,
                    backup_id: str | None = None) -> None:
    digest = _hash_file(partial)
    size = partial.stat().st_size
    partial.replace(final)
    _atomic_write(_sidecar(final), f"{digest}  {final.name}\n".encode("ascii"))
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "filename": final.name,
                "kind": kind, "sha256": digest, "size": size}
    if backup_id is not None:
        manifest["backup_id"] = backup_id
    _atomic_write(_manifest(final), json.dumps(manifest, sort_keys=True).encode("utf-8"))
    if not _verify_manifest(final):
        raise RuntimeError("La publicación atómica no superó la verificación.")


def _safe_backup_id(value: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{32}", str(value or "")):
        raise ValueError("Identificador de conjunto de respaldo inválido.")
    return value


def _media_relative(path: Path, root: Path) -> str:
    relative = path.relative_to(root).as_posix()
    if not (MEDIA_KEY_RE.fullmatch(relative) or MEDIA_MARKER_RE.fullmatch(relative)):
        raise ValueError("El almacenamiento contiene una ruta no administrada.")
    return relative


def _media_inventory(root: Path) -> list[dict]:
    root = root.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("La raíz de medios debe ser un directorio regular.")
    inventory: list[dict] = []
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            if path.is_symlink():
                raise ValueError("No se admiten enlaces en el almacenamiento de medios.")
            continue
        stat = path.lstat()
        if path.is_symlink() or not path.is_file() or stat.st_nlink != 1:
            raise ValueError("No se admiten enlaces ni archivos especiales en medios.")
        relative = _media_relative(path, root)
        if stat.st_size > MEDIA_MAX_FILE_BYTES:
            raise ValueError("Un recurso de medios excede el límite permitido.")
        total += stat.st_size
        if total > MEDIA_MAX_TOTAL_BYTES or len(inventory) >= MEDIA_MAX_FILES:
            raise ValueError("El conjunto de medios excede los límites permitidos.")
        digest = _hash_file(path)
        # Detect a concurrent rewrite. New files are harmless; mutations of an
        # inventoried object make this attempt fail closed and retry later.
        after = path.stat()
        if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise RuntimeError("Un recurso cambió durante el inventario de respaldo.")
        inventory.append({"path": relative, "size": stat.st_size, "sha256": digest})
    known = {item["path"] for item in inventory}
    for item in inventory:
        if MEDIA_MARKER_RE.fullmatch(item["path"]):
            asset = re.sub(r"/\.(?=[^/]+\.retained\.json$)", "/", item["path"])
            asset = asset.removesuffix(".retained.json")
            if asset not in known:
                raise ValueError("Existe un marcador de retención sin su imagen.")
            try:
                marker = json.loads((root / Path(*PurePosixPath(item["path"]).parts))
                                    .read_text(encoding="utf-8"))
                business_id = int(asset.split("/", 2)[1])
                if (marker.get("schema") != 1 or marker.get("storage_key") != asset
                        or marker.get("business_id") != business_id
                        or not isinstance(marker.get("superseded_by_design_version"), int)
                        or marker["superseded_by_design_version"] <= 0):
                    raise ValueError
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError,
                    TypeError, ValueError) as exc:
                raise ValueError("Un marcador de retención no es válido para su tenant.") from exc
    return inventory


class _MediaArchiveReader:
    """Bounded streaming source; clear media is never materialized on disk."""
    def __init__(self, root: Path, backup_id: str, inventory: list[dict]):
        self.root = root.resolve(strict=True)
        header = json.dumps({"schema": 1, "backup_id": _safe_backup_id(backup_id),
                             "files": inventory}, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")
        if len(header) > 4 * 1024 * 1024:
            raise ValueError("El manifiesto de medios excede el límite permitido.")
        self.prefix = io.BytesIO(MEDIA_MAGIC + struct.pack(">I", len(header)) + header)
        self.inventory = iter(inventory)
        self.current = None
        self.current_info = None

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = CHUNK_SIZE
        output = bytearray()
        while len(output) < size:
            if self.prefix is not None:
                chunk = self.prefix.read(size - len(output))
                output.extend(chunk)
                if chunk:
                    continue
                self.prefix = None
            if self.current is None:
                try:
                    self.current_info = next(self.inventory)
                except StopIteration:
                    break
                path = (self.root / Path(*PurePosixPath(self.current_info["path"]).parts)).resolve()
                if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
                    raise RuntimeError("Un recurso dejó de ser regular durante el respaldo.")
                if path.stat().st_size != self.current_info["size"]:
                    raise RuntimeError("Un recurso cambió después del inventario de respaldo.")
                self.current = path.open("rb")
            chunk = self.current.read(size - len(output))
            output.extend(chunk)
            if not chunk:
                self.current.close()
                self.current = None
                self.current_info = None
        return bytes(output)


def media_backup(source_root: Path, output_dir: Path, key_file: Path,
                 backup_id: str) -> Path:
    """Create an encrypted media component without cleartext host artifacts."""
    backup_id = _safe_backup_id(backup_id)
    inventory = _media_inventory(source_root)
    output_dir.mkdir(parents=True, exist_ok=True)
    final = output_dir / f"j2m-media-{backup_id}.j2mbackup"
    partial = final.with_name(final.name + ".partial")
    try:
        with partial.open("xb") as destination:
            encrypt_stream(_MediaArchiveReader(source_root, backup_id, inventory),
                           destination, load_dpapi_key(key_file))
            destination.flush()
            os.fsync(destination.fileno())
        _publish_bundle(partial, final, "media", backup_id)
        if not verify_backup(final, load_dpapi_key(key_file)):
            raise RuntimeError("El respaldo de medios no superó la autenticación.")
        inspect_media_backup(final, key_file, backup_id)
        return final
    except Exception:
        partial.unlink(missing_ok=True)
        _manifest(final).unlink(missing_ok=True)
        _sidecar(final).unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise


def export_media_archive(source_root: Path, backup_id: str, destination: BinaryIO) -> None:
    reader = _MediaArchiveReader(source_root, _safe_backup_id(backup_id),
                                 _media_inventory(source_root))
    for chunk in iter(lambda: reader.read(CHUNK_SIZE), b""):
        destination.write(chunk)


def media_backup_from_docker(volume: str, image: str, output_dir: Path,
                             key_file: Path, backup_id: str,
                             timeout: int = 900,
                             allow_synthetic_volume: bool = False) -> Path:
    if (volume != "j2m-cutover-ready_j2m-media"
            and not (allow_synthetic_volume
                     and re.fullmatch(r"j2m-8e1b-synthetic-[a-z0-9-]+", volume))):
        raise ValueError("El volumen operativo debe declararse explícitamente.")
    if not re.fullmatch(r"j2m-interactive:[0-9a-f]{40}", image):
        raise ValueError("La imagen debe estar fijada a un SHA aprobado exacto.")
    backup_id = _safe_backup_id(backup_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    final = output_dir / f"j2m-media-{backup_id}.j2mbackup"
    partial = final.with_name(final.name + ".partial")
    command = ["docker", "run", "--rm", "--network", "none", "--read-only",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
               "--mount", f"type=volume,src={volume},dst=/media,readonly",
               image, "python", "-m", "scripts.selfhost_backup", "export-media",
               "--source-root", "/media", "--backup-id", backup_id]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdout is not None
    timer = threading.Timer(timeout, process.kill)
    timer.start()
    try:
        with partial.open("xb") as destination:
            encrypt_stream(process.stdout, destination, load_dpapi_key(key_file))
            destination.flush()
            os.fsync(destination.fileno())
        if process.wait(timeout=10) != 0:
            raise RuntimeError("El exportador aislado de medios no pudo completar.")
        _publish_bundle(partial, final, "media", backup_id)
        inspect_media_backup(final, key_file, backup_id)
        return final
    except Exception:
        _manifest(final).unlink(missing_ok=True)
        _sidecar(final).unlink(missing_ok=True)
        final.unlink(missing_ok=True)
        raise
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        partial.unlink(missing_ok=True)


class _MediaArchiveExtractor:
    def __init__(self, destination: Path | None, expected_backup_id: str):
        self.destination = destination
        self.expected_backup_id = _safe_backup_id(expected_backup_id)
        self.buffer = bytearray()
        self.header = None
        self.index = 0
        self.remaining = 0
        self.output = None
        self.digest = None

    def write(self, data: bytes) -> int:
        self.buffer.extend(data)
        if self.header is None and len(self.buffer) >= len(MEDIA_MAGIC) + 4:
            if bytes(self.buffer[:len(MEDIA_MAGIC)]) != MEDIA_MAGIC:
                raise ValueError("Formato de archivo de medios no reconocido.")
            length = struct.unpack(">I", self.buffer[len(MEDIA_MAGIC):len(MEDIA_MAGIC) + 4])[0]
            if length <= 0 or length > 4 * 1024 * 1024:
                raise ValueError("Manifiesto de medios inválido.")
            if len(self.buffer) < len(MEDIA_MAGIC) + 4 + length:
                return len(data)
            raw = bytes(self.buffer[len(MEDIA_MAGIC) + 4:len(MEDIA_MAGIC) + 4 + length])
            del self.buffer[:len(MEDIA_MAGIC) + 4 + length]
            self.header = json.loads(raw.decode("utf-8"))
            if (self.header.get("schema") != 1
                    or self.header.get("backup_id") != self.expected_backup_id
                    or not isinstance(self.header.get("files"), list)
                    or len(self.header["files"]) > MEDIA_MAX_FILES):
                raise ValueError("El manifiesto no pertenece al conjunto esperado.")
        if self.header is not None:
            self._consume()
        return len(data)

    def _consume(self) -> None:
        files = self.header["files"]
        while self.index < len(files):
            item = files[self.index]
            if self.output is None:
                relative = str(item.get("path", ""))
                if not (MEDIA_KEY_RE.fullmatch(relative) or MEDIA_MARKER_RE.fullmatch(relative)):
                    raise ValueError("Ruta de medios inválida en el respaldo.")
                size = item.get("size")
                if not isinstance(size, int) or size < 0 or size > MEDIA_MAX_FILE_BYTES:
                    raise ValueError("Tamaño de medios inválido en el respaldo.")
                if self.destination is None:
                    self.output = _NullWriter()
                else:
                    target = self.destination / Path(*PurePosixPath(relative).parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    self.output = target.open("xb")
                self.digest = hashlib.sha256()
                self.remaining = size
            take = min(self.remaining, len(self.buffer))
            if take:
                chunk = bytes(self.buffer[:take])
                del self.buffer[:take]
                self.output.write(chunk)
                self.digest.update(chunk)
                self.remaining -= take
            if self.remaining:
                return
            self.output.close()
            self.output = None
            if not secrets.compare_digest(self.digest.hexdigest(), str(item.get("sha256", ""))):
                raise ValueError("Un recurso restaurado no coincide con su hash autenticado.")
            self.index += 1

    def finalize(self) -> list[str]:
        if self.header is None or self.output is not None or self.buffer \
                or self.index != len(self.header["files"]):
            raise ValueError("El archivo de medios está incompleto.")
        return [item["path"] for item in self.header["files"]]


def restore_media(backup: Path, destination: Path, key_file: Path,
                  backup_id: str) -> list[str]:
    if destination.exists():
        raise ValueError("El destino de medios ya existe; no se sobrescribirá.")
    key = load_dpapi_key(key_file)
    if not verify_backup(backup, key):
        raise ValueError("El componente de medios no superó la autenticación completa.")
    staging = destination.with_name(destination.name + ".partial-" + secrets.token_hex(4))
    extractor = _MediaArchiveExtractor(staging, backup_id)
    try:
        staging.mkdir(parents=True)
        with backup.open("rb") as source:
            decrypt_stream(source, extractor, key)
        restored = extractor.finalize()
        staging.replace(destination)
        return restored
    finally:
        if extractor.output is not None:
            extractor.output.close()
        if staging.exists():
            shutil.rmtree(staging)


def inspect_media_backup(backup: Path, key_file: Path, backup_id: str) -> list[str]:
    key = load_dpapi_key(key_file)
    if not verify_backup(backup, key):
        raise ValueError("El componente de medios no superó la autenticación completa.")
    verifier = _MediaArchiveExtractor(None, backup_id)
    with backup.open("rb") as source:
        decrypt_stream(source, verifier, key)
    return verifier.finalize()


def import_media_archive(source: BinaryIO, destination: Path, backup_id: str) -> list[str]:
    if any(destination.iterdir()):
        raise ValueError("El volumen aislado de restauración no está vacío.")
    extractor = _MediaArchiveExtractor(destination, backup_id)
    for chunk in iter(lambda: source.read(CHUNK_SIZE), b""):
        extractor.write(chunk)
    return extractor.finalize()


def restore_media_to_docker(backup: Path, key_file: Path, backup_id: str,
                            image: str, timeout: int = 900) -> int:
    if not re.fullmatch(r"j2m-interactive:[0-9a-f]{40}", image):
        raise ValueError("La imagen debe estar fijada a un SHA aprobado exacto.")
    key = load_dpapi_key(key_file)
    if not verify_backup(backup, key):
        raise ValueError("El componente de medios no superó la autenticación completa.")
    volume = "j2m-restore-media-" + secrets.token_hex(6)
    container = "j2m-restore-media-" + secrets.token_hex(6)
    subprocess.run(["docker", "volume", "create", volume], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
    try:
        process = subprocess.Popen(
            ["docker", "run", "--rm", "--name", container, "--network", "none",
             "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
             "--mount", f"type=volume,src={volume},dst=/restore",
             image, "python", "-m", "scripts.selfhost_backup", "import-media",
             "--destination", "/restore", "--backup-id", backup_id],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=False,
        )
        assert process.stdin is not None
        timer = threading.Timer(timeout, process.kill)
        timer.start()
        try:
            with backup.open("rb") as source:
                decrypt_stream(source, process.stdin, key)
            process.stdin.close()
            if process.wait(timeout=10) != 0:
                raise RuntimeError("La restauración aislada de medios falló.")
            assert process.stdout is not None
            stdout = process.stdout.read()
            payload = json.loads(stdout.decode("utf-8"))
            count = int(payload["restored"])
            if count < 0 or count > MEDIA_MAX_FILES:
                raise RuntimeError("La restauración devolvió un recuento inválido.")
            return count
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
        subprocess.run(["docker", "volume", "rm", volume], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)


def _complete_backups(directory: Path, key: bytes) -> list[Path]:
    return sorted((p for p in directory.glob("*.j2mbackup") if verify_backup(p, key)),
                  key=lambda item: item.stat().st_mtime, reverse=True)


def _retain_local_locked(directory: Path, keep: int, key: bytes) -> int:
    backups = _complete_backups(directory, key)
    removed = 0
    for item in backups[max(1, keep):]:
        _manifest(item).unlink(missing_ok=True)
        _sidecar(item).unlink(missing_ok=True)
        item.unlink(missing_ok=True)
        removed += 1
    return removed


def retain_local(directory: Path, key: bytes, keep: int = 48,
                 lock_file: Path | None = None) -> int:
    with exclusive_lock(lock_file or directory.parent / ".operations.lock"):
        return _retain_local_locked(directory, keep, key)


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
                    allow_insecure_synthetic: bool = False,
                    backup_id: str | None = None, retain: bool = True) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    key = load_dpapi_key(key_file)
    root_cert = None
    container_root_cert = None
    configured_root_cert = _read_env_value(env_file, "DATABASE_SSL_ROOT_CERT")
    if configured_root_cert and not allow_insecure_synthetic:
        candidate_root_cert = Path(configured_root_cert).expanduser()
        if candidate_root_cert.is_symlink():
            raise ValueError("DATABASE_SSL_ROOT_CERT debe ser un archivo regular existente.")
        root_cert = candidate_root_cert.resolve(strict=True)
        if not root_cert.is_file():
            raise ValueError("DATABASE_SSL_ROOT_CERT debe ser un archivo regular existente.")
        container_root_cert = "/run/j2m/database-root-ca.crt"
    database_url = _read_database_url(env_file, allow_insecure_synthetic, container_root_cert)
    lock_file = output_dir.parent / ".operations.lock"
    log_path = output_dir.parent / "logs" / "backup.jsonl"
    with exclusive_lock(lock_file):
        for attempt in range(1, retries + 1):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            if backup_id is not None:
                _safe_backup_id(backup_id)
                final = output_dir / f"j2m-postgres-{backup_id}.dump.j2mbackup"
            else:
                final = output_dir / f"j2m-postgres-{stamp}-{secrets.token_hex(3)}.dump.j2mbackup"
            partial = final.with_name(final.name + ".partial")
            container = "j2m-pgdump-" + secrets.token_hex(6)
            command = ["docker", "run", "--rm", "--name", container, "--network", network]
            if root_cert is not None:
                command.extend(["--mount", f"type=bind,src={root_cert},dst={container_root_cert},readonly"])
            command.extend(["-e", "DATABASE_URL", "postgres:17-alpine", "sh", "-eu", "-c",
                       'test -s /etc/ssl/certs/ca-certificates.crt && '
                       'pg_dump --format=custom --no-owner --no-acl "$DATABASE_URL"'])
            child_env = os.environ.copy()
            child_env["DATABASE_URL"] = database_url
            try:
                _run_streaming_dump(command, child_env, partial, key, timeout)
                _publish_bundle(partial, final, "postgresql", backup_id)
                if not verify_backup(final, key):
                    raise RuntimeError("El respaldo publicado no superó autenticación completa.")
                if retain:
                    _retain_local_locked(output_dir, 48, key)
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


def _encrypted_json_bundle(payload: dict, final: Path, key: bytes,
                           kind: str, backup_id: str) -> Path:
    partial = final.with_name(final.name + ".partial")
    try:
        source = io.BytesIO(json.dumps(payload, sort_keys=True,
                                       separators=(",", ":")).encode("utf-8"))
        with partial.open("xb") as destination:
            encrypt_stream(source, destination, key)
            destination.flush()
            os.fsync(destination.fileno())
        _publish_bundle(partial, final, kind, backup_id)
        return final
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _decrypt_json_bundle(path: Path, key: bytes, max_bytes: int = 1024 * 1024) -> dict:
    if not verify_backup(path, key):
        raise ValueError("El manifiesto del conjunto no superó la autenticación.")
    output = io.BytesIO()
    with path.open("rb") as source:
        _, size = decrypt_stream(source, output, key)
    if size > max_bytes:
        raise ValueError("El manifiesto descifrado excede el límite permitido.")
    try:
        payload = json.loads(output.getvalue().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("El manifiesto cifrado no es válido.") from exc
    if not isinstance(payload, dict):
        raise ValueError("El manifiesto cifrado no es válido.")
    return payload


def integral_backup(env_file: Path, media_root: Path | None, output_dir: Path,
                    key_file: Path, retries: int = 3, network: str = "bridge",
                    timeout: int = 900,
                    allow_insecure_synthetic: bool = False,
                    media_volume: str | None = None,
                    media_image: str | None = None) -> Path:
    """Create a DB-first consistent set and publish its authenticated index last.

    Media writes persist and fsync their new object before the database commit,
    while superseded objects are retained. Therefore every managed key visible
    to the completed database snapshot exists when the subsequent media scan
    starts. A concurrent rewrite of an inventoried object fails closed.
    """
    backup_id = secrets.token_hex(16)
    output_dir.mkdir(parents=True, exist_ok=True)
    database = database_backup(env_file, output_dir, key_file, retries, network,
                               timeout, allow_insecure_synthetic, backup_id, retain=False)
    media = None
    index = None
    try:
        if media_root is not None:
            media = media_backup(media_root, output_dir, key_file, backup_id)
        elif media_volume and media_image:
            media = media_backup_from_docker(media_volume, media_image, output_dir,
                                             key_file, backup_id, timeout)
        else:
            raise ValueError("Debe configurarse una fuente de medios explícita.")
        components = []
        for kind, path in (("postgresql", database), ("media", media)):
            components.append({"kind": kind, "filename": path.name,
                               "sha256": _hash_file(path), "size": path.stat().st_size})
        payload = {"schema": 1, "backup_id": backup_id,
                   "created_at": datetime.now(timezone.utc).isoformat(),
                   "consistency": "database-first-append-only-media", "components": components}
        index = output_dir / f"j2m-set-{backup_id}.j2mbackup"
        _encrypted_json_bundle(payload, index, load_dpapi_key(key_file),
                               "integral-set", backup_id)
        if not verify_integral_set(index, key_file):
            raise RuntimeError("El conjunto integral publicado no superó la verificación.")
        _retain_integral_sets(output_dir, key_file, 48)
        return index
    except Exception:
        # A failed integral attempt must not leave components that retention or
        # offsite transfer could mistake for a recoverable set.
        for component in (index, media, database):
            if component is not None:
                _manifest(component).unlink(missing_ok=True)
                _sidecar(component).unlink(missing_ok=True)
                component.unlink(missing_ok=True)
        raise


def verify_integral_set(index: Path, key_file: Path) -> bool:
    try:
        key = load_dpapi_key(key_file)
        payload = _decrypt_json_bundle(index, key)
        backup_id = _safe_backup_id(payload["backup_id"])
        if payload.get("schema") != 1 or len(payload.get("components", [])) != 2:
            return False
        expected = {"postgresql", "media"}
        for component in payload["components"]:
            kind = component["kind"]
            filename = component["filename"]
            if kind not in expected or Path(filename).name != filename:
                return False
            expected.remove(kind)
            path = index.parent / filename
            if (path.stat().st_size != component["size"]
                    or not secrets.compare_digest(_hash_file(path), component["sha256"])
                    or not verify_backup(path, key)):
                return False
            metadata = json.loads(_manifest(path).read_text(encoding="utf-8"))
            if metadata.get("backup_id") != backup_id:
                return False
        return not expected
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


def _retain_integral_sets(directory: Path, key_file: Path, keep: int) -> int:
    key = load_dpapi_key(key_file)
    indexes = sorted((path for path in directory.glob("j2m-set-*.j2mbackup")
                      if verify_integral_set(path, key_file)),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    removed = 0
    for index in indexes[max(1, keep):]:
        payload = _decrypt_json_bundle(index, key)
        members = [index] + [directory / item["filename"] for item in payload["components"]]
        for member in members:
            _manifest(member).unlink(missing_ok=True)
            _sidecar(member).unlink(missing_ok=True)
            member.unlink(missing_ok=True)
        removed += 1
    return removed


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
        key = load_dpapi_key(key_file)
        if state_path.exists() and state_path.read_text(encoding="ascii").strip() == current:
            complete = _complete_backups(output_dir, key)
            if not complete:
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
            encrypt_stream(archive, destination, key)
            destination.flush()
            os.fsync(destination.fileno())
        _publish_bundle(partial, final, "configuration")
        _atomic_write(state_path, current.encode("ascii"))
        _retain_local_locked(output_dir, 30, key)
        return final


def stage_offsite(source_dir: Path, destination_dir: Path, key_file: Path,
                  lock_file: Path | None = None) -> Path:
    """Stage exactly the newest complete, SHA and AEAD verified bundle."""
    with exclusive_lock(lock_file or source_dir.parent / ".operations.lock"):
        key = load_dpapi_key(key_file)
        integral = sorted(source_dir.glob("j2m-set-*.j2mbackup"),
                          key=lambda item: item.stat().st_mtime, reverse=True)
        selected = next((item for item in integral
                         if verify_integral_set(item, key_file)), None)
        members = [selected] if selected else []
        if selected:
            payload = _decrypt_json_bundle(selected, key)
            members.extend(source_dir / item["filename"] for item in payload["components"])
        else:
            selected = next(iter(_complete_backups(source_dir, key)), None)
            members = [selected] if selected else []
        if selected is None:
            raise RuntimeError("No existe un respaldo completo y autenticado para transferir.")
        staging = destination_dir.with_name(destination_dir.name + ".partial-" + secrets.token_hex(4))
        staging.mkdir(parents=True)
        try:
            for member in members:
                for item in (member, _sidecar(member), _manifest(member)):
                    shutil.copy2(item, staging / item.name)
            copied = staging / selected.name
            valid = (verify_integral_set(copied, key_file) if selected.name.startswith("j2m-set-")
                     else verify_backup(copied, key))
            if not valid:
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


def restore_configuration(backup: Path, destination: Path, key_file: Path) -> list[str]:
    """Restore a private configuration archive into a new directory without overwrites."""
    if destination.exists():
        raise ValueError("El directorio de recuperación ya existe; no se sobrescribirá.")
    key = load_dpapi_key(key_file)
    if not verify_backup(backup, key):
        raise ValueError("El respaldo de configuración no superó la autenticación completa.")
    cleartext = io.BytesIO()
    with backup.open("rb") as source:
        decrypt_stream(source, cleartext, key)
    cleartext.seek(0)
    staging = destination.with_name(destination.name + ".partial-" + secrets.token_hex(4))
    restored: list[str] = []
    try:
        staging.mkdir(parents=True)
        with zipfile.ZipFile(cleartext, "r") as archive:
            infos = archive.infolist()
            if not infos or sum(item.file_size for item in infos) > 32 * 1024 * 1024:
                raise ValueError("El archivo de configuración está vacío o excede el límite.")
            for info in infos:
                name = Path(info.filename)
                is_symlink = (info.external_attr >> 16) & 0o170000 == 0o120000
                if (info.is_dir() or is_symlink or "/" in info.filename or "\\" in info.filename
                        or name.name != info.filename or name.is_absolute()):
                    raise ValueError("El respaldo contiene una ruta no permitida.")
                target = staging / name.name
                with target.open("xb") as output:
                    output.write(archive.read(info))
                restored.append(name.name)
        staging.replace(destination)
        return sorted(restored)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _parse_remote_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def remote_retention_plan(inventory: list[dict], max_age_days: int,
                          now: datetime | None = None) -> dict:
    """Return an exact allowlist of remote objects that may be deleted."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=max_age_days)
    groups: dict[str, dict[str, tuple[str, datetime]]] = {}
    integral_groups: dict[str, dict[str, tuple[str, datetime]]] = {}
    suffixes = (".complete.json", ".sha256", "")
    for item in inventory:
        path = str(item.get("Path") or "")
        if not path or "/" in path or "\\" in path or path.startswith("."):
            continue
        base = None
        suffix = None
        for candidate in suffixes:
            marker = ".j2mbackup" + candidate
            if path.endswith(marker):
                base = path[: -len(candidate)] if candidate else path
                suffix = candidate
                break
        if (not base or suffix is None
                or not re.fullmatch(
                    r"j2m-(?:postgres|configuration|set|media)-[A-Za-z0-9._-]+\.j2mbackup",
                    base,
                )):
            continue
        try:
            modified = _parse_remote_time(str(item["ModTime"]))
        except (KeyError, TypeError, ValueError):
            continue
        integral_match = re.fullmatch(
            r"j2m-(set|media|postgres)-([0-9a-f]{32})(?:\.dump)?\.j2mbackup", base
        )
        if integral_match:
            kind, backup_id = integral_match.groups()
            integral_groups.setdefault(backup_id, {})[kind + suffix] = (path, modified)
        else:
            groups.setdefault(base, {})[suffix] = (path, modified)
    required_integral = {kind + suffix for kind in ("set", "media", "postgres")
                         for suffix in suffixes}
    complete_integral = {backup_id: members for backup_id, members in integral_groups.items()
                         if required_integral.issubset(members)}
    if complete_integral:
        protected_id = max(complete_integral,
                           key=lambda value: complete_integral[value]["set.complete.json"][1])
        protected_files = sorted(path for path, _ in complete_integral[protected_id].values())
        deletions = []
        for backup_id, members in integral_groups.items():
            if backup_id == protected_id:
                continue
            if members and all(modified < cutoff for _, modified in members.values()):
                deletions.extend(path for path, _ in members.values())
        return {"protected": f"j2m-set-{protected_id}.j2mbackup",
                "protected_files": protected_files, "delete": sorted(deletions)}
    complete = {
        base: members for base, members in groups.items()
        if all(suffix in members for suffix in suffixes)
    }
    if not complete:
        return {"protected": None, "protected_files": [], "delete": []}
    protected = max(
        complete,
        key=lambda base: complete[base][".complete.json"][1],
    )
    deletions: list[str] = []
    for base, members in groups.items():
        if base == protected:
            continue
        if members and all(modified < cutoff for _, modified in members.values()):
            deletions.extend(path for path, _ in members.values())
    return {"protected": protected,
            "protected_files": sorted(path for path, _ in complete[protected].values()),
            "delete": sorted(deletions)}


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
                     timeout: int = 900, media_backup_path: Path | None = None,
                     backup_id: str | None = None) -> RestoreReport:
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
            # Supabase Vault is a platform-managed extension unavailable in the
            # stock PostgreSQL image. Build the pg_restore TOC only inside the
            # isolated container, remove its extension entries, and exclude the
            # managed vault schema. The decrypted dump is never persisted.
            listing = subprocess.Popen(
                ["docker", "exec", "-i", name, "sh", "-eu", "-c",
                 "pg_restore --list > /tmp/all.list; "
                 "grep -v supabase_vault /tmp/all.list > /tmp/j2m-restore.list; "
                 "test -s /tmp/j2m-restore.list"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            assert listing.stdin is not None
            with backup.open("rb") as encrypted:
                decrypt_stream(encrypted, listing.stdin, key)
            listing.stdin.close()
            if listing.wait(timeout=30) != 0:
                raise RuntimeError("No fue posible preparar la lista de restauración aislada.")
            restore = subprocess.Popen(["docker", "exec", "-i", name, "pg_restore",
                                        "--exit-on-error", "--no-owner", "--no-acl",
                                        "--exclude-schema=vault",
                                        "--use-list=/tmp/j2m-restore.list", "-U",
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
            if media_backup_path is not None:
                if backup_id is None:
                    raise ValueError("Falta el identificador del conjunto de medios.")
                archived = set(inspect_media_backup(media_backup_path, key_file, backup_id))
                referenced = set(filter(None, _psql(
                    name,
                    "SELECT logo_path FROM businesses WHERE logo_path LIKE 'businesses/%' "
                    "UNION SELECT cover_path FROM businesses WHERE cover_path LIKE 'businesses/%'",
                ).splitlines()))
                missing_media = referenced - archived
                if missing_media:
                    raise RuntimeError("El conjunto integral omite medios referenciados por PostgreSQL.")
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
    integral = commands.add_parser("integral")
    integral.add_argument("--env-file", type=Path, default=Path(".env.selfhost"))
    integral_source = integral.add_mutually_exclusive_group(required=True)
    integral_source.add_argument("--media-root", type=Path)
    integral_source.add_argument("--media-volume")
    integral.add_argument("--media-image")
    integral.add_argument("--output-dir", type=Path, default=Path("selfhost-backups/hourly"))
    integral.add_argument("--key-file", type=Path, required=True)
    integral.add_argument("--retries", type=int, default=3)
    integral.add_argument("--timeout", type=int, default=900)
    integral.add_argument("--network", default="bridge", help=argparse.SUPPRESS)
    integral.add_argument("--allow-insecure-synthetic", action="store_true", help=argparse.SUPPRESS)
    verify = commands.add_parser("verify")
    verify.add_argument("backup", type=Path)
    verify.add_argument("--key-file", type=Path, required=True)
    verify_set = commands.add_parser("verify-set")
    verify_set.add_argument("backup", type=Path)
    verify_set.add_argument("--key-file", type=Path, required=True)
    media = commands.add_parser("media")
    media.add_argument("--source-root", type=Path, required=True)
    media.add_argument("--output-dir", type=Path, required=True)
    media.add_argument("--backup-id", required=True)
    media.add_argument("--key-file", type=Path, required=True)
    export_media = commands.add_parser("export-media", help=argparse.SUPPRESS)
    export_media.add_argument("--source-root", type=Path, required=True)
    export_media.add_argument("--backup-id", required=True)
    import_media = commands.add_parser("import-media", help=argparse.SUPPRESS)
    import_media.add_argument("--destination", type=Path, required=True)
    import_media.add_argument("--backup-id", required=True)
    restore_media_parser = commands.add_parser("restore-media")
    restore_media_parser.add_argument("backup", type=Path)
    restore_media_parser.add_argument("--destination", type=Path, required=True)
    restore_media_parser.add_argument("--backup-id", required=True)
    restore_media_parser.add_argument("--key-file", type=Path, required=True)
    restore_media_docker_parser = commands.add_parser("restore-media-docker")
    restore_media_docker_parser.add_argument("backup", type=Path)
    restore_media_docker_parser.add_argument("--backup-id", required=True)
    restore_media_docker_parser.add_argument("--image", required=True)
    restore_media_docker_parser.add_argument("--key-file", type=Path, required=True)
    restore_media_docker_parser.add_argument("--timeout", type=int, default=900)
    prune = commands.add_parser("retain-local")
    prune.add_argument("directory", type=Path)
    prune.add_argument("--keep", type=int, default=48)
    prune.add_argument("--lock-file", type=Path)
    prune.add_argument("--key-file", type=Path, required=True)
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
    restore_config = commands.add_parser("restore-configuration")
    restore_config.add_argument("backup", type=Path)
    restore_config.add_argument("--destination", type=Path, required=True)
    restore_config.add_argument("--key-file", type=Path, required=True)
    retention = commands.add_parser("retention-plan")
    retention.add_argument("--inventory", type=Path, required=True)
    retention.add_argument("--max-age-days", type=int, required=True)
    restore = commands.add_parser("restore-isolated")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--key-file", type=Path, required=True)
    restore.add_argument("--lock-file", type=Path)
    restore.add_argument("--timeout", type=int, default=900)
    restore.add_argument("--media-backup", type=Path)
    restore.add_argument("--backup-id")
    args = parser.parse_args()
    if args.command == "database":
        path = database_backup(args.env_file, args.output_dir, args.key_file,
                               max(1, min(args.retries, 6)), args.network,
                               max(30, min(args.timeout, 7200)), args.allow_insecure_synthetic)
        print(f"Respaldo cifrado creado y verificado: {path.name}")
    elif args.command == "integral":
        path = integral_backup(args.env_file, args.media_root, args.output_dir, args.key_file,
                               max(1, min(args.retries, 6)), args.network,
                               max(30, min(args.timeout, 7200)), args.allow_insecure_synthetic,
                               args.media_volume, args.media_image)
        print(f"Conjunto integral cifrado y verificado: {path.name}")
    elif args.command == "verify":
        if not verify_backup(args.backup, load_dpapi_key(args.key_file)):
            raise ValueError("El respaldo no superó la verificación completa.")
        print("Integridad SHA-256, manifiesto y AES-GCM verificada.")
    elif args.command == "verify-set":
        if not verify_integral_set(args.backup, args.key_file):
            raise ValueError("El conjunto integral no superó la verificación completa.")
        print("Conjunto integral, componentes y vínculo autenticado verificados.")
    elif args.command == "media":
        path = media_backup(args.source_root, args.output_dir, args.key_file, args.backup_id)
        print(f"Medios cifrados y verificados: {path.name}")
    elif args.command == "export-media":
        export_media_archive(args.source_root, args.backup_id, sys.stdout.buffer)
    elif args.command == "import-media":
        restored = import_media_archive(sys.stdin.buffer, args.destination, args.backup_id)
        print(json.dumps({"restored": len(restored)}, sort_keys=True))
    elif args.command == "restore-media":
        restored = restore_media(args.backup, args.destination, args.key_file, args.backup_id)
        print(f"Medios restaurados y verificados: {len(restored)} archivos.")
    elif args.command == "restore-media-docker":
        restored = restore_media_to_docker(args.backup, args.key_file, args.backup_id,
                                           args.image, max(30, min(args.timeout, 7200)))
        print(f"Volumen aislado de medios verificado: {restored} archivos.")
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
    elif args.command == "restore-configuration":
        restored = restore_configuration(args.backup, args.destination, args.key_file)
        print(f"Configuración recuperada y verificada: {len(restored)} archivos.")
    elif args.command == "retention-plan":
        inventory = json.loads(args.inventory.read_text(encoding="utf-8-sig"))
        print(json.dumps(remote_retention_plan(inventory, max(1, args.max_age_days)), sort_keys=True))
    elif args.command == "restore-isolated":
        report = restore_isolated(args.backup, args.key_file, args.lock_file, args.timeout,
                                  args.media_backup, args.backup_id)
        print(f"Restauración aislada verificada; Alembic={report.alembic_revision}; "
              "tablas=4; recuentos comprobados")
    else:
        print(f"Respaldos eliminados por retención: "
              f"{retain_local(args.directory, load_dpapi_key(args.key_file), args.keep, args.lock_file)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
