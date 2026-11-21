import io
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image

from app import create_app
from app.extensions import db
from app.media_storage import LocalMediaStorage, MediaStorageError
from app.models import Business, PlatformAdmin
from app.services.apple_wallet import AppleWalletService
from app.services.google_wallet import GoogleWalletService


def image_bytes(format_name="PNG", size=(4, 3)):
    output = io.BytesIO()
    Image.new("RGB", size, "#336699").save(output, format=format_name)
    return output.getvalue()


class FailingStorage:
    enabled = True

    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = 0

    def save(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 2:
            raise MediaStorageError("synthetic storage failure")
        return self.delegate.save(*args, **kwargs)

    def delete(self, key):
        return self.delegate.delete(key)

    def read_bytes(self, *args, **kwargs):
        return self.delegate.read_bytes(*args, **kwargs)


class PersistentMediaTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app("testing")
        self.app.config.update(
            MEDIA_STORAGE_ROOT=self.temp_dir.name,
            UPLOAD_STORAGE_BACKEND="local",
            PUBLIC_BASE_URL="https://media.example.test",
        )
        self.storage = LocalMediaStorage(self.temp_dir.name)
        self.app.extensions["media_storage"] = self.storage
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.business = Business(name="Tenant Media", slug="tenant-media")
        self.other = Business(name="Tenant Distinto", slug="tenant-distinto")
        admin = PlatformAdmin(name="Admin", email="media-admin@example.test", is_active=True)
        admin.set_password("AdminSeguro123")
        db.session.add_all([self.business, self.other, admin])
        db.session.commit()
        self.admin_id = admin.id
        with self.client.session_transaction() as session:
            session["platform_admin_id"] = self.admin_id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()
        self.temp_dir.cleanup()

    def _create(self, slug="media-new", **files):
        data = {"name": "Nuevo Media", "slug": slug, **files}
        return self.client.post(
            "/admin/business/create", data=data, content_type="multipart/form-data",
        )

    def test_upload_uses_tenant_key_and_public_route(self):
        response = self._create(logo_file=(io.BytesIO(image_bytes()), "brand.png"))
        self.assertEqual(response.status_code, 201)
        business = response.get_json()["business"]
        key = business["logo_path"]
        self.assertRegex(key, rf"^businesses/{business['id']}/logo/[0-9a-f]{{32}}\.png$")
        public = self.client.get(f"/media/{key}")
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.mimetype, "image/png")
        self.assertIn("immutable", public.headers["Cache-Control"])
        self.assertEqual(public.headers["X-Content-Type-Options"], "nosniff")

    def test_supported_formats_are_reencoded_and_unsupported_types_fail(self):
        for index, (format_name, extension, mime) in enumerate((
            ("JPEG", "jpg", "image/jpeg"), ("WEBP", "webp", "image/webp"),
        )):
            with self.subTest(format=format_name):
                response = self._create(
                    slug=f"format-{index}",
                    logo_file=(io.BytesIO(image_bytes(format_name) + b"untrusted-tail"), f"brand.{extension}"),
                )
                self.assertEqual(response.status_code, 201)
                key = response.get_json()["business"]["logo_path"]
                public = self.client.get(f"/media/{key}")
                self.assertEqual(public.mimetype, mime)
                self.assertNotIn(b"untrusted-tail", public.data)
        svg = self._create(
            slug="bad-svg", logo_file=(io.BytesIO(b"<svg><script/></svg>"), "brand.svg"),
        )
        self.assertEqual(svg.status_code, 400)

    def test_path_traversal_tenant_mismatch_and_orphan_are_rejected(self):
        with self.assertRaises(MediaStorageError):
            self.storage.read_bytes("../../secret", expected_business_id=self.business.id)
        key = self.storage.save(self.business.id, "logo", image_bytes(), "png")
        with self.assertRaises(MediaStorageError):
            self.storage.read_bytes(key, expected_business_id=self.other.id)
        self.assertEqual(self.client.get(f"/media/{key}").status_code, 404)
        self.assertEqual(self.client.get("/media/businesses/1/logo/../../secret.png").status_code, 404)

    def test_binary_validation_dimensions_and_partial_failure(self):
        executable = self._create(slug="bad-binary", logo_file=(io.BytesIO(b"MZ"), "brand.png"))
        self.assertEqual(executable.status_code, 400)
        self.app.config["MAX_IMAGE_WIDTH"] = 2
        oversized_dimensions = self._create(
            slug="bad-dimensions", logo_file=(io.BytesIO(image_bytes(size=(3, 1))), "brand.png"),
        )
        self.assertEqual(oversized_dimensions.status_code, 400)
        mismatch = self._create(
            slug="bad-extension", logo_file=(io.BytesIO(image_bytes()), "brand.jpg"),
        )
        self.assertEqual(mismatch.status_code, 400)
        self.app.config["MAX_IMAGE_WIDTH"] = 4096
        self.app.extensions["media_storage"] = FailingStorage(self.storage)
        partial = self._create(
            slug="partial", logo_file=(io.BytesIO(image_bytes()), "logo.png"),
            cover_file=(io.BytesIO(image_bytes()), "cover.png"),
        )
        self.assertEqual(partial.status_code, 503)
        remaining_files = [
            os.path.join(root, name)
            for root, _dirs, names in os.walk(self.temp_dir.name) for name in names
        ]
        self.assertEqual(remaining_files, [])

    def test_database_failure_removes_new_media_and_business(self):
        with patch("app.routes.admin.db.session.commit", side_effect=RuntimeError("synthetic commit failure")):
            response = self._create(
                slug="commit-failure", logo_file=(io.BytesIO(image_bytes()), "brand.png"),
            )
        self.assertEqual(response.status_code, 500)
        self.assertIsNone(Business.query.filter_by(slug="commit-failure").first())
        remaining_files = [
            os.path.join(root, name)
            for root, _dirs, names in os.walk(self.temp_dir.name) for name in names
        ]
        self.assertEqual(remaining_files, [])

    def test_replacement_removes_only_previous_managed_asset(self):
        response = self._create(logo_file=(io.BytesIO(image_bytes()), "brand.png"))
        business = response.get_json()["business"]
        old_key = business["logo_path"]
        update = self.client.post(
            f"/admin/business/{business['id']}/update",
            data={"logo_file": (io.BytesIO(image_bytes(size=(2, 2))), "brand.png")},
            content_type="multipart/form-data", headers={"Accept": "application/json"},
        )
        self.assertEqual(update.status_code, 200)
        new_key = update.get_json()["business"]["logo_path"]
        self.assertNotEqual(old_key, new_key)
        with self.assertRaises(FileNotFoundError):
            self.storage.read_bytes(old_key, expected_business_id=business["id"])
        self.assertTrue(self.storage.read_bytes(new_key, expected_business_id=business["id"]))

    def test_storage_survives_adapter_recreation_and_wallet_consumers(self):
        key = self.storage.save(self.business.id, "logo", image_bytes(), "png")
        self.business.logo_path = key
        db.session.commit()
        recreated = LocalMediaStorage(self.temp_dir.name)
        self.assertEqual(recreated.read_bytes(key, self.business.id), self.storage.read_bytes(key, self.business.id))
        self.assertEqual(
            GoogleWalletService()._logo_uri(self.business),
            f"https://media.example.test/media/{key}",
        )
        png = AppleWalletService._business_asset(self.business, key, (40, 40), fit=False)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_runtime_media_is_excluded_from_source_and_docker_context(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIn("instance/", (root / ".gitignore").read_text(encoding="utf-8"))
        self.assertIn("instance/", (root / ".dockerignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
