"""Create or verify a portable encrypted J2M backup without logging secrets."""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import getpass
import io
from pathlib import Path
import secrets
import zipfile

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


MAGIC = b"J2MBACKUP1"
SALT_BYTES = 16
NONCE_BYTES = 12


def _key(password: bytes, salt: bytes) -> bytes:
    return Scrypt(salt=salt, length=32, n=2**18, r=8, p=1).derive(password)


def encrypt_bytes(cleartext: bytes, password: bytes) -> bytes:
    salt = secrets.token_bytes(SALT_BYTES)
    nonce = secrets.token_bytes(NONCE_BYTES)
    header = MAGIC + salt + nonce
    return header + AESGCM(_key(password, salt)).encrypt(nonce, cleartext, header)


def decrypt_bytes(payload: bytes, password: bytes) -> bytes:
    minimum = len(MAGIC) + SALT_BYTES + NONCE_BYTES + 16
    if len(payload) < minimum or not payload.startswith(MAGIC):
        raise ValueError("Formato de respaldo no reconocido.")
    offset = len(MAGIC)
    salt = payload[offset : offset + SALT_BYTES]
    offset += SALT_BYTES
    nonce = payload[offset : offset + NONCE_BYTES]
    offset += NONCE_BYTES
    header = payload[:offset]
    return AESGCM(_key(password, salt)).decrypt(nonce, payload[offset:], header)


def _password(confirm: bool) -> bytes:
    first = getpass.getpass("Contraseña del respaldo: ").encode("utf-8")
    if len(first) < 16:
        raise ValueError("Use una contraseña de al menos 16 caracteres.")
    if confirm and first != getpass.getpass("Confirmar contraseña: ").encode("utf-8"):
        raise ValueError("Las contraseñas no coinciden.")
    return first


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _unprotect_dpapi(hex_payload: bytes) -> bytes:
    """Decrypt one CurrentUser DPAPI hex blob without writing cleartext to disk."""
    encrypted = bytes.fromhex(hex_payload.decode("ascii").strip())
    source = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
    source_blob = _DataBlob(len(encrypted), source)
    clear_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(source_blob), None, None, None, None, 0, ctypes.byref(clear_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(clear_blob.pbData, clear_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(clear_blob.pbData)


def _portable_text(cleartext: bytes) -> bytes:
    """Normalize PowerShell DPAPI text payloads to portable UTF-8."""
    if len(cleartext) >= 2 and cleartext[1] == 0:
        return cleartext.decode("utf-16-le").encode("utf-8")
    return cleartext


def _archive(source: Path, decrypt_dpapi: bool = False) -> bytes:
    if not source.is_dir():
        raise ValueError("El origen debe ser un directorio.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in sorted(source.rglob("*")):
            if item.is_file():
                name = item.relative_to(source)
                if decrypt_dpapi and item.suffix == ".dpapi":
                    cleartext = _portable_text(_unprotect_dpapi(item.read_bytes()))
                    suffix = ".env" if item.name.startswith("local.env") else ".json"
                    archive.writestr(name.with_suffix(suffix), cleartext)
                else:
                    archive.write(item, name)
    return buffer.getvalue()


def _validate_archive(cleartext: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(cleartext), "r") as archive:
        bad = archive.testzip()
        if bad:
            raise ValueError("El archivo interno está dañado.")
        return len(archive.infolist())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("source", type=Path)
    create.add_argument("output", type=Path)
    create.add_argument(
        "--decrypt-dpapi",
        action="store_true",
        help="Reencripta blobs DPAPI como contenido portable sin escribirlos en claro.",
    )
    verify = subparsers.add_parser("verify")
    verify.add_argument("backup", type=Path)
    args = parser.parse_args()

    if args.command == "create":
        if args.output.exists():
            raise ValueError("El destino ya existe; no se sobrescribirá.")
        password = _password(confirm=True)
        cleartext = _archive(args.source, decrypt_dpapi=args.decrypt_dpapi)
        encrypted = encrypt_bytes(cleartext, password)
        args.output.write_bytes(encrypted)
        count = _validate_archive(decrypt_bytes(encrypted, password))
        print(f"Respaldo cifrado y verificado: {count} archivos.")
        return 0

    count = _validate_archive(decrypt_bytes(args.backup.read_bytes(), _password(confirm=False)))
    print(f"Respaldo verificado: {count} archivos.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
