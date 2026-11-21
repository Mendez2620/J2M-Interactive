"""Persistent, tenant-scoped storage for business branding media."""
from pathlib import Path, PurePosixPath
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

    def save(self, *args, **kwargs):
        raise MediaStorageError("Las subidas están deshabilitadas.")

    def read_bytes(self, *args, **kwargs):
        raise MediaStorageError("El almacenamiento está deshabilitado.")

    def delete(self, key):
        return False


class LocalMediaStorage:
    enabled = True

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

    def read_bytes(self, key, expected_business_id=None):
        return self._path(key, expected_business_id).read_bytes()

    def delete(self, key):
        if not is_managed_media_key(key):
            return False
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True


def build_media_storage(config):
    backend = str(config.get("UPLOAD_STORAGE_BACKEND", "disabled")).strip().lower()
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
