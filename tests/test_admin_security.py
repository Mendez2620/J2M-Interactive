import base64
import io
import os
import time
import unittest
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from unittest.mock import patch

from app import create_app
from app.extensions import db
from app.models import Business, PlatformAdmin, Staff


PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class EventAttributeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.handlers = []

    def handle_starttag(self, _tag, attrs):
        self.handlers.extend(value or "" for name, value in attrs if name.startswith("on"))


class AdminSecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app("testing")
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.business = Business(name="Tenant Uno", slug="tenant-uno")
        self.other_business = Business(name="Tenant Dos", slug="tenant-dos")
        db.session.add_all([self.business, self.other_business])
        admin = PlatformAdmin(name="Global Admin", email="global@j2m.test", is_active=True)
        admin.set_password("AdminSeguro123")
        db.session.add(admin)
        db.session.commit()
        self.admin_id = admin.id

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def login(self):
        with self.client.session_transaction() as session:
            session["platform_admin_id"] = self.admin_id

    def test_anonymous_admin_access_is_rejected(self):
        response = self.client.get("/admin/dashboard", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 401)

    def test_authenticated_admin_pages_render(self):
        self.login()
        dashboard = self.client.get("/admin")
        detail = self.client.get(f"/admin/business/{self.business.id}")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Ajustes", dashboard.data)
        self.assertEqual(detail.status_code, 200)
        self.assertIn(b"Restablecer", detail.data)

    def test_reset_button_does_not_interpolate_username_into_javascript(self):
        self.login()
        username = "x');alert(1);//"
        staff = Staff(business_id=self.business.id, username=username, role="cashier", is_active=True)
        staff.set_password("TenantSeguro123")
        db.session.add(staff)
        db.session.commit()
        response = self.client.get(f"/admin/business/{self.business.id}?tab=staff")
        html = response.get_data(as_text=True)
        self.assertNotIn("onclick=\"openResetModal", html)
        self.assertNotIn("onsubmit=\"return confirm", html)
        self.assertIn("data-reset-password", html)
        self.assertIn("data-confirm-delete", html)
        self.assertIn("data-username=", html)
        parser = EventAttributeParser()
        parser.feed(html)
        self.assertFalse(any("alert(1)" in handler for handler in parser.handlers))
        platform_admin = PlatformAdmin(name="x\");alert(2);//", email="event-audit@j2m.test", is_active=True)
        platform_admin.set_password("AdminSeguro123")
        db.session.add(platform_admin)
        db.session.commit()
        dashboard_parser = EventAttributeParser()
        dashboard_parser.feed(self.client.get("/admin").get_data(as_text=True))
        self.assertFalse(any("alert(2)" in handler for handler in dashboard_parser.handlers))

    def test_login_rejects_wrong_password_and_inactive_account(self):
        bad = self.client.post("/admin/login", data={"email": "global@j2m.test", "password": "incorrecta"})
        self.assertEqual(bad.status_code, 401)
        admin = db.session.get(PlatformAdmin, self.admin_id)
        admin.is_active = False
        db.session.commit()
        inactive = self.client.post("/admin/login", data={"email": admin.email, "password": "AdminSeguro123"})
        self.assertEqual(inactive.status_code, 401)

    def test_super_admin_cookie_stays_short_and_old_cookie_is_rejected(self):
        response = self.client.post(
            "/admin/login", data={"email":"global@j2m.test", "password":"AdminSeguro123"},
            base_url="https://localhost",
        )
        self.assertEqual(response.status_code, 302)
        cookie = next(value for value in response.headers.getlist("Set-Cookie") if value.startswith("session="))
        expires_text = cookie.split("Expires=", 1)[1].split(";", 1)[0]
        remaining = (parsedate_to_datetime(expires_text) - datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(remaining, 3500); self.assertLessEqual(remaining, 3600)
        self.assertEqual(int(self.app.permanent_session_lifetime.total_seconds()), 3600)

        with self.client.session_transaction() as browser_session:
            data = dict(browser_session)
        serializer = self.app.session_interface.get_signing_serializer(self.app)
        with patch("itsdangerous.timed.time.time", return_value=time.time() - 3601):
            old_cookie = serializer.dumps(data)
        self.client.set_cookie("session", old_cookie, domain="localhost")
        denied = self.client.get("/admin", base_url="https://localhost")
        self.assertIn(denied.status_code, {302, 401})
        self.assertIn("/admin/login", denied.location or "")

    def test_tenant_staff_cannot_become_platform_admin(self):
        staff = Staff(business_id=self.business.id, username="tenant-admin", role="admin", is_active=True)
        staff.set_password("TenantSeguro123")
        db.session.add(staff)
        db.session.commit()
        with self.client.session_transaction() as session:
            session["staff_id"] = staff.id
        response = self.client.get("/admin/dashboard", headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 401)

    def test_admin_creation_edit_duplicate_and_password_validation(self):
        self.login()
        weak = self.client.post("/admin/users/create", json={"name": "Dos", "email": "dos@j2m.test", "password": "debil"})
        self.assertEqual(weak.status_code, 400)
        created = self.client.post("/admin/users/create", json={"name": "Dos", "email": "dos@j2m.test", "phone": "555", "password": "SeguraNueva123", "password_confirmation": "SeguraNueva123"})
        self.assertEqual(created.status_code, 201)
        admin_id = created.get_json()["admin"]["id"]
        duplicate = self.client.post("/admin/users/create", json={"name": "Duplicado", "email": "dos@j2m.test", "password": "SeguraNueva123"})
        self.assertEqual(duplicate.status_code, 409)
        old_hash = db.session.get(PlatformAdmin, admin_id).password_hash
        edited = self.client.post(f"/admin/users/edit/{admin_id}", json={"name": "Dos Editado", "email": "dos@j2m.test", "phone": "777", "password": ""})
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(db.session.get(PlatformAdmin, admin_id).password_hash, old_hash)

    def test_staff_password_reset_preserves_identity_and_tenant(self):
        self.login()
        staff = Staff(business_id=self.other_business.id, username="cajero", role="cashier", is_active=True)
        staff.set_password("AnteriorSeguro123")
        db.session.add(staff)
        db.session.commit()
        staff_id, business_id = staff.id, staff.business_id
        response = self.client.post(f"/admin/staff/{staff_id}/reset-password", json={"password": "NuevaSegura456", "password_confirmation": "NuevaSegura456"})
        self.assertEqual(response.status_code, 200)
        same_staff = db.session.get(Staff, staff_id)
        self.assertEqual(same_staff.business_id, business_id)
        self.assertTrue(same_staff.check_password("NuevaSegura456"))

    def test_valid_image_upload_and_dangerous_file_rejection(self):
        self.login()
        valid = self.client.post("/admin/business/create", data={"name": "Con Logo", "slug": "con-logo", "loyalty_type": "stamps", "stamps_reward_limit": "5", "logo_file": (io.BytesIO(PNG_1PX), "logo.png")}, content_type="multipart/form-data")
        self.assertEqual(valid.status_code, 201)
        created = valid.get_json()["business"]
        self.assertTrue(created["logo_path"].startswith("uploads/"))
        updated = self.client.post(f"/admin/business/{created['id']}/update", data={"cover_file": (io.BytesIO(PNG_1PX), "cover.png")}, content_type="multipart/form-data", headers={"Accept": "application/json"})
        self.assertEqual(updated.status_code, 200)
        self.assertTrue(updated.get_json()["business"]["cover_path"].startswith("uploads/"))
        public_page = self.client.get("/join/con-logo").get_data(as_text=True)
        self.assertIn(created["logo_path"], public_page)
        self.assertIn(updated.get_json()["business"]["cover_path"], public_page)
        dangerous = self.client.post("/admin/business/create", data={"name": "Malo", "slug": "malo", "logo_file": (io.BytesIO(b"MZ executable"), "payload.png")}, content_type="multipart/form-data")
        self.assertEqual(dangerous.status_code, 400)

    def test_oversized_image_is_rejected(self):
        self.login()
        oversized = PNG_1PX + (b"0" * (5 * 1024 * 1024))
        response = self.client.post("/admin/business/create", data={"name": "Grande", "slug": "grande", "logo_file": (io.BytesIO(oversized), "large.png")}, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 400)

    def test_two_five_mb_images_fit_request_limit_and_partial_failure_leaves_no_files(self):
        self.login()
        upload_dir = self.app.config["UPLOAD_FOLDER"]
        os.makedirs(upload_dir, exist_ok=True)
        before = set(os.listdir(upload_dir))
        large_png = PNG_1PX + (b"0" * (5 * 1024 * 1024 - len(PNG_1PX)))
        accepted = self.client.post("/admin/business/create", data={"name": "Dos imágenes", "slug": "dos-imagenes", "logo_file": (io.BytesIO(large_png), "logo.png"), "cover_file": (io.BytesIO(large_png), "cover.png")}, content_type="multipart/form-data")
        self.assertEqual(accepted.status_code, 201)
        after_success = set(os.listdir(upload_dir))
        self.assertEqual(len(after_success - before), 2)
        rejected = self.client.post("/admin/business/create", data={"name": "Parcial", "slug": "parcial", "logo_file": (io.BytesIO(PNG_1PX), "logo.png"), "cover_file": (io.BytesIO(b"not an image"), "cover.png")}, content_type="multipart/form-data")
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(set(os.listdir(upload_dir)), after_success)

    def test_loyalty_validation_for_create_and_update(self):
        self.login()
        invalid_stamps = self.client.post("/admin/business/create", json={"name": "Invalid", "slug": "invalid", "loyalty_type": "stamps", "stamps_reward_limit": 0})
        self.assertEqual(invalid_stamps.status_code, 400)
        invalid_points = self.client.post(f"/admin/business/{self.business.id}/update", json={"loyalty_type": "points", "points_per_currency": 0})
        self.assertEqual(invalid_points.status_code, 400)
        self.assertEqual(db.session.get(Business, self.business.id).loyalty_type, "stamps")
        original_name = self.business.name
        for invalid in (float("nan"), float("inf"), float("-inf"), -1, 0, "abc"):
            with self.subTest(program="points", value=invalid):
                response = self.client.post(f"/admin/business/{self.business.id}/update", json={"name": "No debe persistir", "loyalty_type": "points", "points_per_currency": invalid})
                self.assertEqual(response.status_code, 400)
                current = db.session.get(Business, self.business.id)
                self.assertEqual(current.name, original_name)
                self.assertEqual(current.loyalty_type, "stamps")
        for invalid in (float("nan"), float("inf"), -1, 0, 1.5, "1.5", "abc", True):
            with self.subTest(program="stamps", value=invalid):
                response = self.client.post(f"/admin/business/{self.business.id}/update", json={"name": "No debe persistir", "loyalty_type": "stamps", "stamps_reward_limit": invalid})
                self.assertEqual(response.status_code, 400)
                current = db.session.get(Business, self.business.id)
                self.assertEqual(current.name, original_name)
                self.assertEqual(current.stamps_reward_limit, 10)

    def test_csrf_is_enforced_when_enabled(self):
        self.login()
        self.app.config["CSRF_ENABLED"] = True
        rejected = self.client.post("/admin/users/create", json={"name": "Sin CSRF", "email": "csrf@j2m.test", "password": "SeguraNueva123"})
        self.assertEqual(rejected.status_code, 400)
        with self.client.session_transaction() as session:
            session["csrf_token"] = "known-token"
        accepted = self.client.post("/admin/users/create", json={"name": "Con CSRF", "email": "csrf@j2m.test", "password": "SeguraNueva123"}, headers={"X-CSRFToken": "known-token"})
        self.assertEqual(accepted.status_code, 201)
        blocked_channel = self.client.post(f"/admin/business/{self.business.id}/access/mobile")
        self.assertEqual(blocked_channel.status_code, 400)
        enabled_channel = self.client.post(f"/admin/business/{self.business.id}/access/mobile",
                                           headers={"X-CSRFToken": "known-token", "Accept": "application/json"})
        self.assertEqual(enabled_channel.status_code, 200)
        self.assertTrue(enabled_channel.get_json()["enabled"])


if __name__ == "__main__":
    unittest.main()
