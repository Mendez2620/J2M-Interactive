"""Persistent, tenant-scoped storage for business branding media."""
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
import json
import os
import re
import tempfile
import uuid

from flask import current_app, url_for


MANAGED_MEDIA_RE = re.compile(
    r"^businesses/(?P<business_id>[1-9]\d*)/(?P<kind>logo|cover)/"
    r"(?P<name>[0-9a-f]{32})\.(?P<extension>png|jpg|webp)$"
)
LEGACY_UPLOAD_RE = re.compile(r"^uploads/[0-9A-Za-z._-]+\.(?:png|jpg|jpeg|webp)$", re.IGNORECASE)


class MediaStorageError(RuntimeError):
    pass


def parse_managed_media_key(key):
    match = MANAGED_MEDIA_RE.fullmatch(str(key or ""))
    if not match:
        raise MediaStorageError("Clave de recurso no válida.")
    return match.groupdict()


def is_managed_media_key(key):
    return MANAGED_MEDIA_RE.fullmatch(str(key or "")) is not None


class DisabledMediaStorage:
    enabled = False
    read_enabled = False

    def save(self, *args, **kwargs):
        raise MediaStorageError("Las subidas están deshabilitadas.")

    def read_bytes(self, *args, **kwargs):
        raise MediaStorageError("El almacenamiento está deshabilitado.")

    def delete(self, key):
        return False

    def is_retained(self, *args, **kwargs):
        return False

    def retain(self, *args, **kwargs):
        raise MediaStorageError("El almacenamiento está deshabilitado.")

    def release_retention(self, *args, **kwargs):
        return False


class LocalMediaStorage:
    enabled = True
    read_enabled = True

    def __init__(self, root):
        self.root = Path(root).resolve()

    def _path(self, key, expected_business_id=None):
        parts = parse_managed_media_key(key)
        if expected_business_id is not None and int(parts["business_id"]) != int(expected_business_id):
            raise MediaStorageError("El recurso no pertenece al negocio solicitado.")
        relative = PurePosixPath(key)
        if relative.is_absolute() or ".." in relative.parts:
            raise MediaStorageError("Ruta de recurso no válida.")
        path = (self.root / Path(*relative.parts)).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise MediaStorageError("Ruta de recurso no válida.") from exc
        return path

    def save(self, business_id, kind, content, extension):
        if not isinstance(business_id, int) or business_id <= 0 or kind not in {"logo", "cover"}:
            raise MediaStorageError("Destino de recurso no válido.")
        extension = str(extension).lower()
        key = f"businesses/{business_id}/{kind}/{uuid.uuid4().hex}.{extension}"
        destination = self._path(key, expected_business_id=business_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="xb", dir=destination.parent, prefix=".upload-", delete=False
            ) as output:
                temporary = Path(output.name)
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
            try:
                destination.chmod(0o640)
            except OSError:
                pass
        except Exception:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise
        return key

    def _retention_path(self, key, expected_business_id=None):
        media_path = self._path(key, expected_business_id)
        return media_path.with_name(f".{media_path.name}.retained.json")

    def retain(self, key, expected_business_id, superseded_by_design_version):
        media_path = self._path(key, expected_business_id)
        if not media_path.is_file():
            raise MediaStorageError("No se encontró el recurso que debe conservarse.")
        try:
            design_version = int(superseded_by_design_version)
        except (TypeError, ValueError) as exc:
            raise MediaStorageError("Versión de branding no válida.") from exc
        if design_version <= 0:
            raise MediaStorageError("Versión de branding no válida.")
        marker = self._retention_path(key, expected_business_id)
        payload = json.dumps({
            "schema": 1,
            "storage_key": key,
            "business_id": int(expected_business_id),
            "superseded_by_design_version": design_version,
            "retired_at": datetime.now(timezone.utc).isoformat(),
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="xb", dir=marker.parent, prefix=".retention-", delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, marker)
            try:
                marker.chmod(0o640)
            except OSError:
                pass
        except Exception:
            if temporary:
                temporary.unlink(missing_ok=True)
            raise
        return payload

    def retention_metadata(self, key, expected_business_id):
        marker = self._retention_path(key, expected_business_id)
        try:
            raw = marker.read_bytes()
            if len(raw) > 2048:
                return None
            data = json.loads(raw.decode("utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        expected = parse_managed_media_key(key)
        if (not isinstance(data, dict) or data.get("schema") != 1
                or data.get("storage_key") != key
                or data.get("business_id") != int(expected_business_id)
                or int(expected["business_id"]) != int(expected_business_id)
                or not isinstance(data.get("superseded_by_design_version"), int)
                or data["superseded_by_design_version"] <= 0
                or not isinstance(data.get("retired_at"), str)):
            return None
        return data

    def is_retained(self, key, expected_business_id):
        try:
            return (
                self._path(key, expected_business_id).is_file()
                and self.retention_metadata(key, expected_business_id) is not None
            )
        except MediaStorageError:
            return False

    def release_retention(self, key, expected_business_id):
        try:
            self._retention_path(key, expected_business_id).unlink()
        except FileNotFoundError:
            return False
        return True

    def read_bytes(self, key, expected_business_id=None):
        return self._path(key, expected_business_id).read_bytes()

    def delete(self, key):
        if not is_managed_media_key(key):
            return False
        path = self._path(key)
        self._retention_path(key).unlink(missing_ok=True)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True


def build_media_storage(config):
    backend = str(config.get("MEDIA_STORAGE_BACKEND", "disabled")).strip().lower()
    if backend == "disabled":
        return DisabledMediaStorage()
    if backend == "local":
        return LocalMediaStorage(config["MEDIA_STORAGE_ROOT"])
    raise RuntimeError("UPLOAD_STORAGE_BACKEND no es compatible.")


def get_media_storage():
    return current_app.extensions["media_storage"]


def branding_asset_url(path):
    if is_managed_media_key(path):
        return url_for("media.public_media", asset_key=path)
    if LEGACY_UPLOAD_RE.fullmatch(str(path or "")):
        return url_for("static", filename=path)
    return None


def absolute_branding_url(path, public_base_url):
    base = str(public_base_url or "").rstrip("/")
    if not base:
        return ""
    if is_managed_media_key(path):
        return f"{base}/media/{path}"
    if LEGACY_UPLOAD_RE.fullmatch(str(path or "")):
        return f"{base}/static/{path}"
    return ""
