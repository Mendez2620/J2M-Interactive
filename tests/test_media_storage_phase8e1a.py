import io
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from PIL import Image

from app import create_app
from tests.admin_session_fixture import issue_for
from app.extensions import db
from app.media_storage import LocalMediaStorage, MediaStorageError
from app.models import Business, PlatformAdmin
from app.services.apple_wallet import AppleWalletService
from app.services.google_wallet import GoogleWalletService
from app.config import TestingConfig


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


class ConcurrentRetentionStorage:
    """Coordinate a post-retention commit failure with a competing request."""

    enabled = True
    read_enabled = True

    def __init__(self, delegate, retained, allow_failure, success_completed, release_called):
        self.delegate = delegate
        self.retained = retained
        self.allow_failure = allow_failure
        self.success_completed = success_completed
        self.release_called = release_called

    def retain(self, *args, **kwargs):
        result = self.delegate.retain(*args, **kwargs)
        if threading.current_thread().name == "failing-branding":
            self.retained.set()
            if not self.allow_failure.wait(5):
                raise MediaStorageError("Tiempo de espera sintético.")
        return result

    def release_retention(self, *args, **kwargs):
        self.release_called.set()
        if threading.current_thread().name == "failing-branding":
            self.success_completed.wait(5)
        return self.delegate.release_retention(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.delegate, name)


class PersistentMediaTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app("testing")
        self.app.config.update(
            MEDIA_STORAGE_ROOT=self.temp_dir.name,
            MEDIA_STORAGE_BACKEND="local",
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
            session["platform_admin_session"] = issue_for(session["platform_admin_id"])

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
        self.storage._retention_path(key, self.business.id).write_text("{}", encoding="utf-8")
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

    def test_update_rollback_removes_new_asset_but_conserves_safe_retention(self):
        response = self._create(logo_file=(io.BytesIO(image_bytes()), "brand.png"))
        business = response.get_json()["business"]
        old_key = business["logo_path"]
        with patch("app.routes.admin.db.session.commit", side_effect=RuntimeError("synthetic commit failure")):
            update = self.client.post(
                f"/admin/business/{business['id']}/update",
                data={"logo_file": (io.BytesIO(image_bytes(size=(2, 2))), "brand.png")},
                content_type="multipart/form-data", headers={"Accept": "application/json"},
            )
        self.assertEqual(update.status_code, 500)
        db.session.expire_all()
        self.assertEqual(db.session.get(Business, business["id"]).logo_path, old_key)
        marker = self.storage.retention_metadata(old_key, business["id"])
        self.assertEqual(marker["business_id"], business["id"])
        self.assertEqual(self.client.get(f"/media/{old_key}").status_code, 200)
        files = [
            path for root, _dirs, names in os.walk(self.temp_dir.name)
            for name in names if not name.endswith(".json")
            for path in [os.path.join(root, name)]
        ]
        self.assertEqual(len(files), 1)

    def test_replacement_retains_previous_managed_asset_for_wallet(self):
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
        self.assertTrue(self.storage.read_bytes(old_key, expected_business_id=business["id"]))
        self.assertTrue(self.storage.read_bytes(new_key, expected_business_id=business["id"]))
        metadata = self.storage.retention_metadata(old_key, business["id"])
        self.assertEqual(
            metadata["superseded_by_design_version"],
            update.get_json()["business"]["wallet_design_version"],
        )
        self.assertEqual(self.client.get(f"/media/{old_key}").status_code, 200)

    def test_storage_error_never_discloses_internal_host_path(self):
        response = self._create(logo_file=(io.BytesIO(image_bytes()), "brand.png"))
        business = response.get_json()["business"]
        with patch.object(
            self.storage, "retain",
            side_effect=MediaStorageError(r"C:\private\host\media\secret.png"),
        ):
            update = self.client.post(
                f"/admin/business/{business['id']}/update",
                data={"logo_file": (io.BytesIO(image_bytes(size=(2, 2))), "brand.png")},
                content_type="multipart/form-data", headers={"Accept": "application/json"},
            )
        self.assertEqual(update.status_code, 503)
        body = update.get_data(as_text=True).lower()
        self.assertNotIn("private", body)
        self.assertNotIn("secret.png", body)

    def test_existing_google_wallet_image_survives_save_before_publication(self):
        response = self._create(logo_file=(io.BytesIO(image_bytes()), "brand.png"))
        business_id = response.get_json()["business"]["id"]
        business = db.session.get(Business, business_id)
        old_key = business.logo_path
        old_uri = GoogleWalletService()._logo_uri(business)
        update = self.client.post(
            f"/admin/business/{business_id}/update",
            data={"logo_file": (io.BytesIO(image_bytes(size=(2, 2))), "brand.png")},
            content_type="multipart/form-data", headers={"Accept": "application/json"},
        )
        self.assertEqual(update.status_code, 200)
        db.session.expire_all()
        business = db.session.get(Business, business_id)
        self.assertNotEqual(GoogleWalletService()._logo_uri(business), old_uri)
        self.assertTrue(old_uri.endswith(f"/media/{old_key}"))
        self.assertEqual(self.client.get(old_uri.removeprefix("https://media.example.test")).status_code, 200)

    def test_disabled_uploads_do_not_disable_existing_media_reads(self):
        key = self.storage.save(self.business.id, "logo", image_bytes(), "png")
        self.business.logo_path = key
        db.session.commit()
        self.app.config["UPLOAD_STORAGE_BACKEND"] = "disabled"
        self.assertEqual(self.client.get(f"/media/{key}").status_code, 200)
        blocked = self._create(
            slug="blocked-write", logo_file=(io.BytesIO(image_bytes()), "brand.png"),
        )
        self.assertEqual(blocked.status_code, 503)

    def test_metadata_is_removed_transparency_preserved_and_final_size_limited(self):
        for index, (format_name, extension, has_alpha) in enumerate((
            ("JPEG", "jpg", False), ("PNG", "png", True), ("WEBP", "webp", True),
        )):
            with self.subTest(format=format_name):
                source = io.BytesIO()
                exif = Image.Exif()
                exif[0x010E] = "private-description"
                mode = "RGBA" if has_alpha else "RGB"
                color = (1, 2, 3, 17) if has_alpha else (1, 2, 3)
                Image.new(mode, (8, 8), color).save(
                    source, format=format_name, exif=exif,
                    icc_profile=b"private-icc-profile",
                )
                response = self._create(
                    slug=f"metadata-{index}",
                    logo_file=(io.BytesIO(source.getvalue()), f"brand.{extension}"),
                )
                self.assertEqual(response.status_code, 201)
                key = response.get_json()["business"]["logo_path"]
                with Image.open(io.BytesIO(self.storage.read_bytes(key))) as normalized:
                    self.assertEqual(len(normalized.getexif()), 0)
                    self.assertNotIn("exif", normalized.info)
                    self.assertNotIn("icc_profile", normalized.info)
                    if has_alpha:
                        self.assertEqual(normalized.convert("RGBA").getpixel((0, 0))[3], 17)

        self.app.config["MAX_NORMALIZED_IMAGE_BYTES"] = 32
        final_too_large = self._create(
            slug="normalized-too-large",
            logo_file=(io.BytesIO(image_bytes()), "brand.png"),
        )
        self.assertEqual(final_too_large.status_code, 400)

    @unittest.skipUnless(os.getenv("TEST_POSTGRES_URL"), "PostgreSQL efímero no disponible")
    def test_concurrent_replacements_serialize_and_failed_request_cannot_remove_retention(self):
        postgres_url = os.environ["TEST_POSTGRES_URL"].replace(
            "postgresql://", "postgresql+psycopg2://", 1,
        )
        with tempfile.TemporaryDirectory() as media_root, \
             patch.object(TestingConfig, "SQLALCHEMY_DATABASE_URI", postgres_url):
            app = create_app("testing")
            app.config.update(
                MEDIA_STORAGE_ROOT=media_root, MEDIA_STORAGE_BACKEND="local",
                UPLOAD_STORAGE_BACKEND="local", PUBLIC_BASE_URL="https://media.example.test",
            )
            storage = LocalMediaStorage(media_root)
            retained = threading.Event()
            allow_failure = threading.Event()
            completed_success = threading.Event()
            release_called = threading.Event()
            app.extensions["media_storage"] = ConcurrentRetentionStorage(
                storage, retained, allow_failure, completed_success, release_called,
            )
            with app.app_context():
                db.session.remove()
                db.drop_all()
                db.create_all()
                business = Business(name="Concurrent Media", slug="concurrent-media")
                admin = PlatformAdmin(name="Admin", email="concurrent-media@example.test", is_active=True)
                admin.set_password("AdminSeguro123")
                db.session.add_all([business, admin])
                db.session.flush()
                old_key = storage.save(business.id, "logo", image_bytes(), "png")
                business.logo_path = old_key
                db.session.commit()
                business_id, admin_id = business.id, admin.id

            results = {}
            def replace(name, size):
                client = app.test_client()
                with client.session_transaction() as session:
                    session["platform_admin_id"] = admin_id
                    session["platform_admin_session"] = issue_for(session["platform_admin_id"])
                response = client.post(
                    f"/admin/business/{business_id}/update",
                    data={"logo_file": (io.BytesIO(image_bytes(size=size)), "brand.png")},
                    content_type="multipart/form-data", headers={"Accept": "application/json"},
                )
                results[name] = (response.status_code, response.get_data(as_text=True))
                if name == "success":
                    completed_success.set()

            real_commit = db.session.commit

            def controlled_commit():
                if threading.current_thread().name == "failing-branding":
                    raise RuntimeError("synthetic post-retention commit failure")
                return real_commit()

            failing = threading.Thread(target=replace, args=("failure", (2, 2)), name="failing-branding")
            succeeding = threading.Thread(target=replace, args=("success", (3, 3)), name="successful-branding")
            with patch.object(db.session, "commit", side_effect=controlled_commit):
                failing.start()
                self.assertTrue(retained.wait(5))
                succeeding.start()
                time.sleep(0.2)
                self.assertFalse(completed_success.is_set(), "La segunda solicitud no esperó el lock PostgreSQL")
                allow_failure.set()
                failing.join(10)
                succeeding.join(10)
            self.assertFalse(failing.is_alive())
            self.assertFalse(succeeding.is_alive())
            self.assertEqual(results["failure"][0], 500)
            self.assertEqual(results["success"][0], 200)
            self.assertFalse(release_called.is_set(), "Un rollback no debe eliminar retenciones")

            with app.app_context():
                db.session.remove()
                current = db.session.get(Business, business_id)
                current_key = current.logo_path
                self.assertNotEqual(current_key, old_key)
                self.assertEqual(current.wallet_design_version, 2)
                self.assertTrue(storage.is_retained(old_key, business_id))
                self.assertTrue(storage.read_bytes(old_key, business_id))
                self.assertTrue(storage.read_bytes(current_key, business_id))
                client = app.test_client()
                self.assertEqual(client.get(f"/media/{old_key}").status_code, 200)
                self.assertEqual(client.get(f"/media/{current_key}").status_code, 200)
                db.session.remove()
                db.drop_all()

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

    def test_compose_scopes_media_volume_to_explicit_project_and_writer(self):
        root = Path(__file__).resolve().parents[1]
        compose = (root / "compose.selfhost.yml").read_text(encoding="utf-8")
        self.assertIn("name: ${J2M_COMPOSE_PROJECT_NAME:-j2m-cutover-ready}", compose)
        self.assertIn("- j2m-media:/app/instance/media", compose)
        self.assertNotIn("name: ${J2M_MEDIA_VOLUME", compose)
        self.assertEqual(compose.count("j2m-media:/app/instance/media"), 1)


if __name__ == "__main__":
    unittest.main()
